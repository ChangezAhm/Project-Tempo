"""The ONLY LLM step in population: text-only meaning matching.

Given the template's distinct metrics and the deterministic source catalogue
(labels only — no values, no images), ask the model which source series *means*
which template metric, and whether the sign convention differs. That's it. No
addresses, no numbers, no scale, no periods — those are all decided deterministically
in binding.py from facts Aspose already knows.

This is what makes runaway cost structurally impossible for the populate path:
one cheap (Sonnet) text call (chunked if huge), under the run's spend cap, with a
dry-run estimate available before a single token is sent.
"""

from __future__ import annotations

import json
import logging

from app.llm import MODEL_MAP, guarded_stream
from app.population.catalogue import Series
from app.population.cost import estimate_call_usd
from app.population.schema import MappingOut, MetricMap

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You map a consulting TEMPLATE's metrics to a SOURCE workbook's data series by "
    "FINANCIAL MEANING. You get the template metrics and a catalogue of source series "
    "(each a labelled row). For EACH template metric, choose one STATUS and fill the "
    "fields for it. The template and the source almost NEVER share the exact same "
    "breakdown — your job is to reconcile them intelligently, not to give up.\n"
    "\nSTATUS — pick exactly one per metric:\n"
    "- direct: a single source series means the same thing -> set series_id.\n"
    "- aggregate: the metric is the EXACT arithmetic SUM of several source series and no "
    "single source series already means it (e.g. 'Total Revenue' when the source lists "
    "only 'Revenue - NA / EMEA / APAC') -> series_id=first, also_series_ids=the rest. "
    "Exact; filled automatically.\n"
    "- reconcile: the source HAS the same economic amount but cut DIFFERENTLY than the "
    "template wants — the data EXISTS, just organised differently. Assign the best source "
    "data to THIS line (one series, or a sum via also_series_ids) and write a short "
    "`assumption`. Use reconcile when:\n"
    "   * the source COMBINES what the template SPLITS — e.g. one 'Depreciation & "
    "amortisation' line: reconcile it onto Depreciation (assumption 'source combines D&A; "
    "assigned to depreciation'); mark Amortisation unavailable (note 'included in the "
    "combined D&A on Depreciation').\n"
    "   * the source SPLITS on a DIFFERENT axis — e.g. opex by function (S&M, G&A) vs the "
    "template's by-nature Staff/Other: SUM the functional lines (also_series_ids) onto the "
    "template's RESIDUAL/other line (assumption 'source splits opex by function; summed "
    "into other opex'); mark the more specific line (Staff Costs) unavailable (note "
    "'source does not separate staff costs; included in other opex').\n"
    "   * the source gives a TOTAL where the template wants a COMPONENT — assign the total "
    "to the dominant component; mark the siblings unavailable.\n"
    "  Reconcile fills are PROVISIONAL: they are flagged and sent to the user to confirm. "
    "The value still comes from a real source series — NEVER invent numbers. NEVER map the "
    "same source amount into two template lines (no double counting — that is why the "
    "sibling line is marked unavailable).\n"
    "- needs_decision: the source HAS related data but assigning it would require a human "
    "choice that would be WRONG to guess — e.g. a blended TOTAL where two template "
    "components each EXCLUDE part of it (source 'Total turnover' vs template Recurring "
    "Revenue [excludes one-off] AND Non-recurring Revenue [excludes recurring]): you cannot "
    "put the total in either without breaking its qualification. -> series_id=null, and put "
    "the QUESTION and the options in `assumption` (e.g. 'source has only total turnover — "
    "split into recurring/non-recurring how, or assign all to one?'). We ASK the user; we do "
    "NOT fill it.\n"
    "- unavailable: the source has NO data for this line, not even at a different cut "
    "(e.g. a cash-flow line when the source has no cash-flow statement) -> series_id=null "
    "and put a SPECIFIC reason in `note`.\n"
    "\nRULES:\n"
    "- Match meaning, not wording: 'Total revenue'=='Net sales'; 'COGS'=='Cost of sales'.\n"
    "- A metric may carry `def:`/`qualifies:` from the template. For a DIRECT match they "
    "are binding: if a source series fails the qualification, do not map it directly. You "
    "MAY still reconcile with an explicit assumption.\n"
    "- A TEMPLATE CONTEXT block, when present, carries sponsor-confirmed rules and answered "
    "review questions — it is AUTHORITATIVE. If it states how to reconcile a line, follow "
    "it exactly (that is a human decision that must win over your own).\n"
    "- set sign_flip=true only when conventions differ (source costs +ve, template -ve).\n"
    "- confidence in [0,1]. For direct/aggregate, <0.6 is dropped. reconcile is kept "
    "(provisional) but still score it honestly.\n"
    "- NEVER output values, cell addresses, scales, or currencies — only the mapping.\n"
    'Return ONLY JSON: {"mappings":[{"metric":"...","status":"direct|aggregate|reconcile|'
    'needs_decision|unavailable","series_id":"...|null","also_series_ids":[],'
    '"assumption":"...|null","sign_flip":false,"confidence":0.0,"note":"..."}]}'
)

_BATCH = 80  # template metrics per call; the full series catalogue rides along each time


def _series_lines(catalogue: dict[str, Series]) -> str:
    lines = []
    for s in catalogue.values():
        sample = ", ".join(f"{v:g}" for v in s.sample) if s.sample else ""
        unit = []
        if s.unit.currency:
            unit.append(s.unit.currency)
        if s.unit.kind and s.unit.kind != "unknown":
            unit.append(s.unit.kind)
        meta = f" [{'/'.join(unit)}]" if unit else ""
        lines.append(f"{s.id} | {s.sheet} | {s.label}{meta}" + (f" | e.g. {sample}" if sample else ""))
    return "\n".join(lines)


def _metric_lines(metrics: list[dict]) -> str:
    from app.population.units import resolve_unit
    out = []
    for m in metrics:
        label = m.get("label") or m.get("metric")
        unit = m.get("unit")
        kind = resolve_unit(unit).kind
        meta = f" | unit: {unit}" if unit else ""
        if kind != "unknown":
            meta += f" ({kind})"
        sign = m.get("sign_convention")
        if sign:   # the template's own convention — informs the sign_flip guess
            meta += f" | sign: {str(sign)[:60]}"
        # The L3 business logic — what the line MEANS and what QUALIFIES to be in
        # it — is exactly what separates 'Adjusted' from 'Reported' EBITDA. The
        # mapper was deciding on labels alone while this sat unused in the model.
        if m.get("definition"):
            meta += f" | def: {str(m['definition'])[:90]}"
        if m.get("qualification_criteria"):
            meta += f" | qualifies: {str(m['qualification_criteria'])[:110]}"
        out.append(f"{m.get('metric')} | {label}{meta}")
    return "\n".join(out)


def _user_text(metrics: list[dict], series_block: str, context: str = "") -> str:
    ctx = f"TEMPLATE CONTEXT (authoritative — sponsor-confirmed):\n{context}\n\n" if context else ""
    return (
        f"{ctx}"
        "SOURCE SERIES (id | sheet | label [unit] | samples):\n"
        f"{series_block}\n\n"
        "TEMPLATE METRICS to map (key | label | unit | def | qualifies):\n"
        f"{_metric_lines(metrics)}\n\n"
        "Return the JSON now."
    )


def estimate_mapping_usd(metrics: list[dict], catalogue: dict[str, Series],
                         max_tokens: int = 8000, context: str = "") -> float:
    """Dry-run cost: what the whole mapping step would cost before sending anything."""
    series_block = _series_lines(catalogue)
    total = 0.0
    for i in range(0, len(metrics), _BATCH):
        chunk = metrics[i:i + _BATCH]
        chars = len(_SYSTEM) + len(_user_text(chunk, series_block, context))
        total += estimate_call_usd(MODEL_MAP, chars, max_tokens)
    return round(total, 4)


def _parse(text: str) -> list[MetricMap]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    return MappingOut(**json.loads(text[start:end + 1])).mappings


def _map_chunk(chunk: list[dict], series_block: str, max_tokens: int,
               context: str = "") -> list[MetricMap]:
    """One mapping batch, with ONE corrective retry when the reply doesn't parse
    (or parses to nothing for a non-empty chunk). Raises after the retry fails —
    the caller decides whether that kills the run."""
    user = _user_text(chunk, series_block, context)
    _, text = guarded_stream(model=MODEL_MAP, system=_SYSTEM, content=user,
                             max_tokens=max_tokens, est_input_chars=len(_SYSTEM) + len(user))
    try:
        maps = _parse(text)
        if maps:
            return maps
        err = "reply contained no mappings"
    except Exception as e:  # noqa: BLE001 — malformed JSON from the model
        err = str(e)
    logger.warning("mapping batch didn't parse (%s) — one corrective retry", err)
    messages = [
        {"role": "user", "content": user},
        {"role": "assistant", "content": text[:4000]},
        {"role": "user", "content": (
            f"That reply was not usable ({err}). Return ONLY the JSON object "
            '{"mappings":[...]} for the TEMPLATE METRICS above — no prose, no fences.'
        )},
    ]
    _, text = guarded_stream(model=MODEL_MAP, system=_SYSTEM, messages=messages,
                             max_tokens=max_tokens)
    maps = _parse(text)
    if not maps:
        raise RuntimeError("mapping batch unusable after corrective retry")
    return maps


def map_metrics(metrics: list[dict], catalogue: dict[str, Series],
                max_tokens: int = 8000, context: str = "") -> tuple[list[MetricMap], int]:
    """Run the mapping (chunked). ``context`` is the template's business-context
    block (sponsor notes, answered review questions, strict rules) — authoritative
    knowledge the mapper must honor. Returns (mappings, failed_batches). A batch
    whose retry also fails is dropped LOUDLY — logged and counted, so the run
    report can say why coverage is low — instead of either silently vanishing or
    killing a paid run. Guarded by the run's spend cap inside guarded_stream; a
    SpendCapExceeded still aborts everything."""
    from app.population.cost import SpendCapExceeded

    if not metrics or not catalogue:
        return [], 0
    series_block = _series_lines(catalogue)
    out: list[MetricMap] = []
    failed = 0
    for i in range(0, len(metrics), _BATCH):
        chunk = metrics[i:i + _BATCH]
        try:
            out.extend(_map_chunk(chunk, series_block, max_tokens, context))
        except SpendCapExceeded:
            raise
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("mapping batch %d-%d failed after retry — %d metrics unmapped",
                             i, i + len(chunk), len(chunk))
    return out, failed
