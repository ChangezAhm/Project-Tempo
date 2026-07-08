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

from app.llm import MODEL_MAP, guarded_stream
from app.population.catalogue import Series
from app.population.cost import estimate_call_usd
from app.population.schema import MappingOut, MetricMap

_SYSTEM = (
    "You map a consulting TEMPLATE's metrics to a SOURCE workbook's data series by "
    "FINANCIAL MEANING only. You are given template metrics and a catalogue of source "
    "series (each a labelled row). For each template metric, pick the single source "
    "series id that means the same thing, or null if none fits.\n"
    "RULES:\n"
    "- Match meaning, not wording: 'Total revenue' == 'Net sales'; 'COGS' == 'Cost of "
    "sales'. Do NOT match a subtotal to a line item or vice versa.\n"
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
        out.append(f"{m.get('metric')} | {label}{meta}")
    return "\n".join(out)


def _user_text(metrics: list[dict], series_block: str) -> str:
    return (
        "SOURCE SERIES (id | sheet | label [unit] | samples):\n"
        f"{series_block}\n\n"
        "TEMPLATE METRICS to map (key | label | unit):\n"
        f"{_metric_lines(metrics)}\n\n"
        "Return the JSON now."
    )


def estimate_mapping_usd(metrics: list[dict], catalogue: dict[str, Series],
                         max_tokens: int = 8000) -> float:
    """Dry-run cost: what the whole mapping step would cost before sending anything."""
    series_block = _series_lines(catalogue)
    total = 0.0
    for i in range(0, len(metrics), _BATCH):
        chunk = metrics[i:i + _BATCH]
        chars = len(_SYSTEM) + len(_user_text(chunk, series_block))
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


def map_metrics(metrics: list[dict], catalogue: dict[str, Series],
                max_tokens: int = 8000) -> list[MetricMap]:
    """Run the mapping (chunked). Returns a flat list of MetricMap. Guarded by the
    run's spend cap inside guarded_stream — a SpendCapExceeded will abort the run."""
    if not metrics or not catalogue:
        return []
    series_block = _series_lines(catalogue)
    out: list[MetricMap] = []
    for i in range(0, len(metrics), _BATCH):
        chunk = metrics[i:i + _BATCH]
        user = _user_text(chunk, series_block)
        _, text = guarded_stream(model=MODEL_MAP, system=_SYSTEM, content=user,
                                 max_tokens=max_tokens, est_input_chars=len(_SYSTEM) + len(user))
        out.extend(_parse(text))
    return out
