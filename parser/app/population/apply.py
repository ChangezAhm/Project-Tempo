"""Deterministic apply: read the real source values per the mapping and bind
them to the template's cells. The LLM never touches the numbers — this does,
exactly, from the source snapshot, with full attribution.
"""

from __future__ import annotations

import re

from app.population.schema import FilledCell, PopulationMapping, PopulationResult
from app.raw_extraction.column_utils import column_index, column_letter

_CELL = re.compile(r"^([A-Z]+)(\d+)$")
_WS = re.compile(r"\s+")


def _rc(addr: str) -> tuple[int, int] | None:
    m = _CELL.match((addr or "").strip().upper())
    return (column_index(m.group(1)), int(m.group(2))) if m else None


def _norm(s) -> str:
    return _WS.sub(" ", str(s or "").strip()).lower()


def _ref(f: dict) -> dict:
    return {"template_cell": f.get("cell"), "metric": f.get("canonical_metric") or f.get("metric_label"),
            "period_index": f.get("period_index"), "scenario": f.get("scenario")}


def apply_mapping(facts: list[dict], source_snapshot: dict, mapping: PopulationMapping) -> PopulationResult:
    """facts = template input facts (data points); source_snapshot = parsed source
    workbook; mapping = the LLM-produced location map. Returns filled cells +
    unmatched, with values read deterministically from the source."""
    # index source cell values by (sheet, row, col)
    sval: dict[tuple[str, int, int], object] = {}
    for s in source_snapshot.get("sheets", []):
        for c in s.get("cells", []):
            rc = _rc(c.get("address", ""))
            if rc:
                sval[(s["name"], rc[1], rc[0])] = c.get("value")

    matches: dict[tuple[str, str | None], object] = {}
    for m in mapping.metric_matches:
        matches[(_norm(m.template_metric), m.scenario)] = m
    pcol: dict[tuple[str, int], int] = {}
    for pa in mapping.period_aligns:
        pcol[(pa.source_sheet, pa.period_index)] = pa.source_col

    filled: list[FilledCell] = []
    unmatched: list[dict] = []
    for f in facts:
        mkey = _norm(f.get("canonical_metric") or f.get("metric_label"))
        m = matches.get((mkey, f.get("scenario")))
        if not m and f.get("scenario") in (None, "actual", "unknown"):
            # a scenario-agnostic source match (e.g. an actuals-only file) fills
            # actual/unknown slots, never budget/forecast — don't bleed actuals into budget.
            m = matches.get((mkey, None))
        if not m:
            unmatched.append({"reason": "no metric match", **_ref(f)})
            continue
        col = pcol.get((m.source_sheet, f.get("period_index")))
        if col is None:
            unmatched.append({"reason": "no period alignment", **_ref(f)})
            continue
        raw = sval.get((m.source_sheet, m.source_row, col))
        if raw in (None, ""):
            unmatched.append({"reason": "empty source cell", **_ref(f)})
            continue
        try:
            value = float(raw) * m.unit_scale * (-1.0 if m.sign_flip else 1.0)
        except (TypeError, ValueError):
            value = raw   # non-numeric passes through unchanged
        filled.append(FilledCell(
            template_sheet=f["sheet_name"], template_cell=f["cell"], value=value, raw_source_value=raw,
            source_sheet=m.source_sheet, source_cell=f"{column_letter(col)}{m.source_row}",
            metric=f.get("canonical_metric") or f.get("metric_label"),
            period_index=f.get("period_index"), scenario=f.get("scenario"), confidence=m.confidence,
        ))

    return PopulationResult(filled=filled, unmatched=unmatched, summary={
        "facts": len(facts), "filled": len(filled), "unmatched": len(unmatched),
    })
