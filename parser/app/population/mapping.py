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
    "FINANCIAL MEANING only. You are given template metrics and a catalogue of source "
    "series (each a labelled row). For each template metric, pick the single source "
    "series id that means the same thing, or null if none fits.\n"
    "RULES:\n"
    "- Match meaning, not wording: 'Total revenue' == 'Net sales'; 'COGS' == 'Cost of "
    "sales'. Do NOT match a subtotal to a line item or vice versa.\n"
    "- A metric may carry `def:` (what the line MEANS) and `qualifies:` (what belongs "
    "in it and what does NOT) — read from the template itself. These OVERRIDE "
    "label-based intuition: if a source series fails the qualification criteria, do "
    "not map it, whatever the label says.\n"
    "- A TEMPLATE CONTEXT block, when present, carries sponsor-confirmed rules and "
    "answered review questions. It is authoritative over your own judgment.\n"
    "- set sign_flip=true only when conventions differ (e.g. source shows costs as "
    "positive but the template expects them negative).\n"
    "- confidence in [0,1]: be honest; <0.6 will be dropped rather than risk a wrong number.\n"
    "- NEVER output values, cell addresses, scales, or currencies — only the mapping.\n"
    'Return ONLY JSON: {"mappings":[{"metric":"...","series_id":"...|null",'
    '"sign_flip":false,"confidence":0.0,"note":"..."}]}'
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
