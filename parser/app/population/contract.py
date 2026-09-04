"""Template Contract — learned decisions that replay deterministically
(docs/Fill-Plan-Architecture.md §2.6).

A question the verifier files carries a structured ``check_spec.decision``
({metric, field, proposal, scope, source_fingerprint}). When the user answers
it (one tap on the suggested answer, or a short text), the answer is parsed
back into a concrete field value here and OVERLAID onto every future plan
before verification — the plan cannot contradict a confirmed decision, and the
same question is never asked twice (the content-addressed item_key already
suppresses re-filing).

SCOPES (§2.6): ``template`` decisions (rollup, mapping confirmations) apply on
every run of this template; ``source_format`` decisions (units — "this pack is
in USD'000") apply only when the incoming source belongs to the same FAMILY,
fingerprinted by its sheet-name set — stable across a pack's monthly editions,
different for an unrelated workbook. GLOBAL promotion is deliberately not a
code path: it happens by adding a prior (with multi-template evidence) via the
rule ledger.

This is the deterministic layer the product wants: built from confirmed user
intent — never promoted to a global rule by code.
"""

from __future__ import annotations

import hashlib
import logging

from app.population.schema import MetricMap

logger = logging.getLogger(__name__)


def source_fingerprint(snapshot: dict) -> str:
    """Source FAMILY identity: hash of the sorted sheet names. A reporting
    pack's monthly editions share it; a different system's export won't."""
    names = sorted((s.get("name") or "") for s in (snapshot or {}).get("sheets", []))
    return hashlib.sha1("|".join(names).encode("utf-8")).hexdigest()[:16]

_CONFIRM = ("yes", "approve", "approved", "confirm", "confirmed", "ok", "keep", "correct")

# fields a decision may set on a SeriesFill
_FIELDS = {"rollup", "source_unit", "target_unit", "sign_flip", "confirmed", "scenario",
           # template-level (metric='*'), consumed by the EXECUTOR not the plan:
           # {"budget": "forecast"} = slots demanding budget accept forecast columns
           "scenario_equivalence"}


def decision_spec(metric: str, field: str, proposal=None, *, scope: str = "template",
                  fingerprint: str | None = None) -> dict:
    """check_spec payload for a filed question, so the answer parses back.
    scope='source_format' pins the decision to the filing run's source family."""
    dec: dict = {"metric": metric, "field": field, "proposal": proposal, "scope": scope}
    if scope == "source_format" and fingerprint:
        dec["source_fingerprint"] = fingerprint
    return {"decision": dec}


def parse_answer(field: str, answer: str, proposal=None):
    """A short human answer -> a concrete field value (None = unparseable)."""
    t = (answer or "").strip().lower()
    if not t:
        return None
    if field == "rollup":
        if "end" in t or "point" in t:
            return "end"
        if "avg" in t or "average" in t or "mean" in t:
            return "avg"
        if "sum" in t or "total" in t or "add" in t:
            return "sum"
        return None
    if field in ("source_unit", "target_unit"):
        return answer.strip()[:40]
    if field == "sign_flip":
        if t.startswith(_CONFIRM):
            return proposal
        if "flip" in t or "negative" in t or "invert" in t:
            return True
        if "no" in t or "positive" in t or "as-is" in t:
            return False
        return None
    if field == "confirmed":
        return True if t.startswith(_CONFIRM) else None
    if field == "scenario":
        for s in ("actual", "budget", "forecast"):
            if s in t:
                return s
        return None
    if field == "scenario_equivalence":
        # "yes — use forecast for budget columns" (the one-tap) -> the proposal;
        # free text naming both scenarios also parses; a refusal doesn't apply.
        if t.startswith(_CONFIRM) or ("use" in t and "forecast" in t):
            return proposal
        if "no" in t or "blank" in t:
            return None
        return None
    return None


def load_decisions(version_id: str, fingerprint: str | None = None) -> dict[str, dict]:
    """metric -> {field: value} from ANSWERED review items carrying a decision
    spec. ``fingerprint`` = the CURRENT source's family fingerprint; a
    source_format-scoped decision applies only when it matches (a unit answer
    for last month's pack must not silently rescale an unrelated file).
    Best-effort — no decisions is a normal state, never an error."""
    try:
        from app import supabase_client as sb
        items = sb.list_review_items(version_id)
    except Exception as e:  # noqa: BLE001
        logger.info("contract decisions unavailable (%s)", e)
        return {}
    out: dict[str, dict] = {}
    for it in items:
        if it.get("status") != "answered":
            continue
        dec = ((it.get("check_spec") or {}).get("decision") or {})
        metric, field = dec.get("metric"), dec.get("field")
        if not metric or field not in _FIELDS:
            continue
        if dec.get("scope") == "source_format" and dec.get("source_fingerprint") != fingerprint:
            continue
        answer = ((it.get("resolution") or {}).get("answer") or "")
        val = parse_answer(field, answer, dec.get("proposal"))
        if val is None and answer.strip().lower().startswith(_CONFIRM):
            val = dec.get("proposal")
        if val is not None:
            out.setdefault(metric, {})[field] = val
    return out


def apply_decisions(fills: list[MetricMap], decisions: dict[str, dict]) -> int:
    """Overlay confirmed decisions onto the plan (in place). The contract WINS
    over the planner — that is learned determinism, the good kind. Returns the
    number of fields applied."""
    if not decisions:
        return 0
    applied = 0
    for m in fills:
        dec = decisions.get(m.metric)
        if not dec:
            continue
        changed = 0   # per fill — the note marks only fills a decision touched
        for field, val in dec.items():
            if field == "scenario_equivalence":
                continue   # executor-level; extracted by the caller, not a plan field
            if field == "confirmed":
                if val and m.confidence < 0.9:
                    m.confidence = 0.9   # user confirmed the mapping — floor cleared
                    changed += 1
                continue
            if getattr(m, field, None) != val:
                setattr(m, field, val)
                changed += 1
        note = "[contract: confirmed decision applied]"
        if changed and note not in (m.note or ""):
            m.note = f"{m.note} {note}".strip() if m.note else note
        applied += changed
    return applied
