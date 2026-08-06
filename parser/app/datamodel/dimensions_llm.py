"""LLM enrichment pass — auto-fill the interpretive dimensions the deterministic
derivation leaves `unknown` (basis flow-vs-point-in-time, canonical metric name,
config-vs-data), grounded in the sheet/role/label context Layer 3 already
extracted.

Works on the *distinct metrics* (~hundreds), not every fact, batched ~80 per
call so a big workbook can't blow past max_tokens and lose the whole run to a
truncated reply. Output is written as
`created_by='llm-enrichment'` corrections, so it reuses the corrections
machinery: it re-applies on every derive (cached — no repeat LLM call), is
**fill-only** (never overrides a deterministic value), and is overridden by
user corrections. Re-running replaces the prior LLM batch.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict

from app import supabase_client as sb
from app.datamodel.persist import derive_and_persist, get_data_model
from app.datamodel.schema import Basis
from app.llm import MODEL_SMART, guarded_stream
from app.population.cost import (
    SpendCapExceeded, SpendGuard, default_onboarding_cap_usd, set_guard,
)
from app.understanding.per_sheet import _extract_json, to_strict_schema

logger = logging.getLogger(__name__)
_LLM = "llm-enrichment"
_BATCH = 80   # metrics per call — keeps one reply's JSON inside max_tokens

# What the LLM may legally write into a correction patch. basis must be a real
# Basis member ('unknown' is the no-op default, filtered separately); category
# corrections are limited to the re-categorisations schema.py documents ('data'
# is the default, so only config/exclude are patches) — 'sourced'/'computed' are
# derived deterministically from formulas and never LLM-assigned.
_VALID_BASIS = {b.value for b in Basis} - {Basis.unknown.value}
_VALID_CATEGORY = {"config", "exclude", "data"}   # 'data' = an explicit override of a
                                                  # lexicon-guessed category (merge gates it)


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DimAssignment(_M):
    id: int
    canonical_metric: str | None     # standard snake_case PE/finance id, or null
    basis: str                       # point_in_time | flow | ytd | trailing | unknown
    category: str                    # data | config | exclude


class DimAssignments(_M):
    assignments: list[DimAssignment]


_SCHEMA = to_strict_schema(DimAssignments)

SYSTEM = (
    "You enrich the data model of a private-equity reporting template. For each METRIC you are given its "
    "sheet, the sheet's role, the row label, and its unit. Assign three things, using standard PE/finance "
    "judgement grounded in the label and sheet context:\n"
    "1. canonical_metric — a standard snake_case identifier (e.g. revenue, gross_profit, ebitda, net_debt, "
    "fixed_assets, trade_receivables, cash, capex). null if it is not a recognisable standard metric.\n"
    "2. basis — point_in_time for a balance-sheet stock measured at period end; flow for a P&L or cash-flow "
    "amount over the period; ytd; trailing for LTM; unknown if genuinely unclear (e.g. a ratio/selector).\n"
    "3. category — data for a real reporting data point the template expects filled; config for a "
    "selector/toggle/setting/override control input; exclude for something that is not a data point at "
    "all. Some metrics carry current_category/category_source: when category_source starts with "
    "'lexicon' a word-list GUESSED the category — confirm it or override it (answering data REVERSES a "
    "wrong lexicon call, e.g. a real line item whose label merely resembles a placeholder).\n"
    "Return ONLY JSON matching the schema; echo each metric's id."
)


# max_tokens is sized for one ~80-metric batch (a few KB of JSON + adaptive
# thinking), not the whole workbook — the old 32k single-call budget is gone.
def _call(user_text: str, max_tokens: int = 16000):
    # Routed through the choke point — spend guard + tracing, no hand-rolled copy.
    return guarded_stream(model=MODEL_SMART, system=SYSTEM, content=user_text,
                          max_tokens=max_tokens, site="dimension_enrichment")


def _classify_batch(items: list[dict], brief: str | None = None) -> tuple[list[DimAssignment], tuple[int, int]]:
    """One LLM call for one batch of metrics. Returns (assignments, (in_tok, out_tok))."""
    preamble = f"WORKBOOK CONTEXT: {brief}\n\n" if brief else ""
    user_text = (
        preamble
        + f"Metrics to classify ({len(items)}):\n{json.dumps(items)}\n\n"
        "## OUTPUT\nReturn ONLY a JSON object matching this schema:\n" + json.dumps(_SCHEMA)
    )
    msg, text = _call(user_text)
    try:
        parsed = DimAssignments.model_validate(json.loads(_extract_json(text)))
    except Exception as e:  # one corrective retry
        logger.warning("enrichment parse failed (%s); retrying", e)
        msg, text = _call(user_text + f"\n\nThat did not parse ({e}). Return ONLY the corrected JSON.")
        parsed = DimAssignments.model_validate(json.loads(_extract_json(text)))
    return parsed.assignments, (msg.usage.input_tokens, msg.usage.output_tokens)


def _validated_patch(a: DimAssignment) -> dict:
    """LLM strings → correction patch. Values outside the legal enums are dropped
    with a warning rather than persisted as garbage corrections."""
    patch: dict = {}
    if a.canonical_metric:
        patch["canonical_metric"] = a.canonical_metric
    if a.basis and a.basis != "unknown":       # 'unknown' = nothing to fill
        if a.basis in _VALID_BASIS:
            patch["basis"] = a.basis
        else:
            logger.warning("enrichment: dropping invalid basis %r (metric id %d)", a.basis, a.id)
    if a.category:
        # 'data' is kept: it is the explicit override of a lexicon-guessed config
        # (merge lets an LLM patch beat only lexicon-sourced categories, so on an
        # already-data fact it is a no-op).
        if a.category in _VALID_CATEGORY:
            patch["category"] = a.category
        else:
            logger.warning("enrichment: dropping invalid category %r (metric id %d)", a.category, a.id)
    return patch


def enrich(template_id: str) -> dict:
    """Run the enrichment pass and store its assignments as llm corrections."""
    set_guard(SpendGuard(default_onboarding_cap_usd()))   # cap the enrichment call (onboarding-tier)
    dm = get_data_model(template_id, limit=30000)
    if not dm.get("available"):
        raise RuntimeError("No data model yet — run /datamodel first.")
    facts = dm["facts"]
    version_id = dm["template_version_id"]
    roles = {r["sheet_name"]: r["role"] for r in (
        sb.get_client().table("template_sheet_understanding").select("sheet_name,role")
        .eq("template_version_id", version_id).execute().data or [])}
    # workbook usage brief — the model should know it is classifying a TEMPLATE
    # our system populates, not reading a filled report.
    brief = None
    try:
        from app.population.context import usage_brief
        row = (sb.get_client().table("template_understanding").select("understanding")
               .eq("template_version_id", version_id).limit(1).execute().data)
        brief = usage_brief((row[0].get("understanding") or {}) if row else {})
    except Exception as e:  # noqa: BLE001 — brief is best-effort
        logger.info("usage brief unavailable for enrichment (%s)", e)

    # distinct metrics (sheet, label) → context. A lexicon-sourced fact is
    # preferred as the group representative: its category_source is what the
    # prompt shows for confirm/override, and the FIRST fact may lack it.
    metrics: dict[tuple[str, str], dict] = {}
    for f in facts:
        key = (f["sheet_name"], f["metric_label"])
        cur = metrics.get(key)
        if cur is None or (f.get("category_source") and not cur.get("category_source")):
            metrics[key] = {"unit": f.get("unit"), "category": f.get("category"),
                            "category_source": f.get("category_source")}
    idx = {i: key for i, key in enumerate(metrics)}
    items = []
    for i, key in idx.items():
        m = metrics[key]
        item = {"id": i, "sheet": key[0], "role": roles.get(key[0]), "label": key[1], "unit": m["unit"]}
        if m.get("category_source"):   # a lexicon guessed — show it, so the model confirms/overrides
            item["current_category"] = m["category"]
            item["category_source"] = m["category_source"]
        items.append(item)

    # Batched calls: a failed batch (after its retry) is dropped LOUDLY — logged
    # and counted, so the run report can say why enrichment coverage is low —
    # instead of killing a paid run. A blown spend cap still aborts everything.
    assignments: list[DimAssignment] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    failed = 0
    for i in range(0, len(items), _BATCH):
        chunk = items[i:i + _BATCH]
        try:
            got, (in_tok, out_tok) = _classify_batch(chunk, brief)
        except SpendCapExceeded:
            raise
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("enrichment batch %d-%d failed after retry — %d metrics unenriched",
                             i, i + len(chunk), len(chunk))
            continue
        assignments.extend(got)
        usage["input_tokens"] += in_tok
        usage["output_tokens"] += out_tok

    rows = []
    for a in assignments:
        key = idx.get(a.id)
        if not key:
            continue
        patch = _validated_patch(a)
        # a 'data' verdict is only a correction when it REVERSES a lexicon call —
        # on plain-data metrics it would just bloat the corrections table
        if (patch.get("category") == "data"
                and not str(metrics[key].get("category_source") or "").startswith("lexicon")):
            patch.pop("category")
        if patch:
            rows.append({"target": "metric", "match": {"sheet_name": key[0], "metric_label": key[1]},
                         "patch": patch, "note": "LLM enrichment", "created_by": _LLM})

    sb.supersede_corrections_by(template_id, _LLM)   # replace any prior enrichment batch
    written = sb.add_corrections(template_id, rows)
    return {
        "metrics": len(items),
        "corrections_written": written,
        "failed_batches": failed,
        "usage": usage,
    }


def enrich_and_persist(template_id: str) -> dict:
    """Run the enrichment pass, then re-derive so the assignments take effect."""
    enrichment = enrich(template_id)
    datamodel = derive_and_persist(template_id)
    return {"enrichment": enrichment, "datamodel": datamodel}
