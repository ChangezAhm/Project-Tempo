"""Rebuild a ParsedWorkbook from a stored snapshot — no Aspose re-parse.

Lets the structure layer (and impact) re-derive from the durable snapshot blob
instead of re-opening the workbook. Only the fields the detectors need are
restored (cells, regions, protection/visibility, formula graph).
"""

from __future__ import annotations

from app.raw_extraction.formula_mapper import build_formula_graph_from_precedents
from app.raw_extraction.schema import CellInfo, DetectedRegion, NamedRange, WorkbookMetadata
from app.raw_extraction.workbook_parser import ParsedSheet, ParsedWorkbook


def reconstruct_workbook_from_snapshot(snap: dict) -> ParsedWorkbook:
    wb = ParsedWorkbook()
    wb.metadata = WorkbookMetadata.model_validate(snap.get("metadata") or {"filename": ""})
    wb.named_ranges = [NamedRange.model_validate(n) for n in snap.get("named_ranges", [])]

    precedents_by_cell: dict[str, list[str]] = {}
    formula_addrs: set[str] = set()
    formula_strings: dict[str, str] = {}
    populated: set[str] = set()

    for sd in snap.get("sheets", []):
        ps = ParsedSheet(name=sd["name"], index=sd["index"], is_hidden=sd.get("is_hidden", False))
        ps.is_protected = sd.get("is_protected", False)
        ps.tab_color = sd.get("tab_color")
        ps.used_max_row = sd.get("used_max_row", 0)
        ps.used_max_col = sd.get("used_max_col", 0)
        ps.frozen_rows = sd.get("frozen_rows", 0)
        ps.frozen_cols = sd.get("frozen_cols", 0)
        ps.print_area = sd.get("print_area")
        ps.was_truncated = sd.get("was_truncated", False)
        ps.row_group_levels = {int(k): v for k, v in (sd.get("row_group_levels") or {}).items()}
        ps.cells = [CellInfo.model_validate(c) for c in sd.get("cells", [])]
        ps.regions = [DetectedRegion.model_validate(r) for r in sd.get("regions", [])]

        for c in ps.cells:
            q = f"{ps.name}!{c.address}"
            populated.add(q)
            if c.formula:
                formula_addrs.add(q)
                formula_strings[q] = c.formula
                if c.precedents:
                    precedents_by_cell[q] = c.precedents

        wb.sheets.append(ps)

    wb.formula_graph = build_formula_graph_from_precedents(
        precedents_by_cell,
        formula_addrs=formula_addrs,
        populated_cells=populated,
        formula_strings=formula_strings,
    )
    return wb
