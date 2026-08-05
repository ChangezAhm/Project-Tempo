"""Template Contract — learned decisions that replay deterministically
(docs/Fill-Plan-Architecture.md §2.6, v1: metric/template scope).

A question the verifier files carries a structured ``check_spec.decision``
({metric, field, proposal}). When the user answers it (one tap on the suggested
answer, or a short text), the answer is parsed back into a concrete field value
here and OVERLAID onto every future plan before verification — the plan cannot
contradict a confirmed decision, and the same question is never asked twice
(the content-addressed item_key already suppresses re-filing).

This is the deterministic layer the product wants: built from confirmed user
intent, scoped to the template — never promoted to a global rule by code.
"""

from __future__ import annotations

import logging

from app.population.schema import MetricMap

logger = logging.getLogger(__name__)

_CONFIRM = ("yes", "approve", "approved", "confirm", "confirmed", "ok", "keep", "correct")

# fields a decision may set on a SeriesFill
_FIELDS = {"rollup", "source_unit", "target_unit", "sign_flip", "confirmed", "scenario"}


def decision_spec(metric: str, field: str, proposal=None) -> dict:
    """check_spec payload for a filed question, so the answer parses back."""
    return {"decision": {"metric": metric, "field": field, "proposal": proposal}}


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
    return None


def load_decisions(version_id: str) -> dict[str, dict]:
    """metric -> {field: value} from ANSWERED review items carrying a decision
    spec. Best-effort — no decisions is a normal state, never an error."""
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
        for field, val in dec.items():
            if field == "confirmed":
                if val and m.confidence < 0.9:
                    m.confidence = 0.9   # user confirmed the mapping — floor cleared
                    applied += 1
                continue
            if getattr(m, field, None) != val:
                setattr(m, field, val)
                applied += 1
        note = "[contract: confirmed decision applied]"
        if applied and (m.note or "") .find(note) < 0:
            m.note = f"{m.note} {note}".strip() if m.note else note
    return applied
