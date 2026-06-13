"""LLM enrichment pass — auto-fill the interpretive dimensions the deterministic
derivation leaves `unknown` (basis flow-vs-point-in-time, canonical metric name,
config-vs-data), grounded in the sheet/role/label context Layer 3 already
extracted.

One Opus call per workbook, working on the *distinct metrics* (~hundreds), not
every fact. Output is written as `created_by='llm-enrichment'` corrections, so
it reuses the corrections machinery: it re-applies on every derive (cached — no
repeat LLM call), is **fill-only** (never overrides a deterministic value), and
is overridden by user corrections. Re-running replaces the prior LLM batch.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict

from app import supabase_client as sb
from app.datamodel.persist import derive_and_persist, get_data_model
from app.llm import MODEL, get_client
from app.understanding.per_sheet import _extract_json, to_strict_schema

logger = logging.getLogger(__name__)
_LLM = "llm-enrichment"


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
    "3. category — data for a real reporting data point; config for a selector/toggle/setting/override "
    "control input; exclude for something that is not a data point at all.\n"
    "Return ONLY JSON matching the schema; echo each metric's id."
)


def _call(user_text: str, max_tokens: int = 32000):
    with get_client().messages.stream(
        model=MODEL, max_tokens=max_tokens, thinking={"type": "adaptive"},
        system=SYSTEM, messages=[{"role": "user", "content": user_text}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"Enrichment truncated at max_tokens={max_tokens} — raise it.")
    return msg, next((b.text for b in msg.content if b.type == "text"), "")


def enrich(template_id: str) -> dict:
    """Run the enrichment pass and store its assignments as llm corrections."""
    dm = get_data_model(template_id, limit=30000)
    if not dm.get("available"):
        raise RuntimeError("No data model yet — run /datamodel first.")
    facts = dm["facts"]
    version_id = dm["template_version_id"]
    roles = {r["sheet_name"]: r["role"] for r in (
        sb.get_client().table("template_sheet_understanding").select("sheet_name,role")
        .eq("template_version_id", version_id).execute().data or [])}

    # distinct metrics (sheet, label) → context
    metrics: dict[tuple[str, str], dict] = {}
    for f in facts:
        key = (f["sheet_name"], f["metric_label"])
        if key not in metrics:
            metrics[key] = {"unit": f.get("unit")}
    idx = {i: key for i, key in enumerate(metrics)}
    items = [{"id": i, "sheet": key[0], "role": roles.get(key[0]), "label": key[1], "unit": metrics[key]["unit"]}
             for i, key in idx.items()]

    user_text = (
        f"Metrics to classify ({len(items)}):\n{json.dumps(items)}\n\n"
        "## OUTPUT\nReturn ONLY a JSON object matching this schema:\n" + json.dumps(_SCHEMA)
    )
    msg, text = _call(user_text)
    try:
        parsed = DimAssignments.model_validate(json.loads(_extract_json(text)))
    except Exception as e:  # one corrective retry
        logger.warning("enrichment parse failed (%s); retrying", e)
        msg, text = _call(user_text + f"\n\nThat did not parse ({e}). Return ONLY the corrected JSON.")
        parsed = DimAssignments.model_validate(json.loads(_extract_json(text)))

    rows = []
    for a in parsed.assignments:
        key = idx.get(a.id)
        if not key:
            continue
        patch: dict = {}
        if a.canonical_metric:
            patch["canonical_metric"] = a.canonical_metric
        if a.basis and a.basis not in ("unknown", None):
            patch["basis"] = a.basis
        if a.category and a.category not in ("data", None):
            patch["category"] = a.category
        if patch:
            rows.append({"target": "metric", "match": {"sheet_name": key[0], "metric_label": key[1]},
                         "patch": patch, "note": "LLM enrichment", "created_by": _LLM})

    sb.supersede_corrections_by(template_id, _LLM)   # replace any prior enrichment batch
    written = sb.add_corrections(template_id, rows)
    return {
        "metrics": len(items),
        "corrections_written": written,
        "usage": {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens},
    }


def enrich_and_persist(template_id: str) -> dict:
    """Run the enrichment pass, then re-derive so the assignments take effect."""
    enrichment = enrich(template_id)
    datamodel = derive_and_persist(template_id)
    return {"enrichment": enrichment, "datamodel": datamodel}
