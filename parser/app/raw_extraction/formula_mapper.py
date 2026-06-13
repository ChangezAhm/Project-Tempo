"""Formula dependency graph from Aspose-resolved precedent ranges.

``build_formula_graph_from_precedents`` takes the per-cell precedent RANGES
that Aspose.Cells resolved during parse (``cell.get_precedents()``) and folds
them into a link / input / output graph. No formula-string parsing is
involved — concrete dependency edges are recovered by intersecting each range
with the cells that actually exist, so a whole-column or 10,000-row reference
never explodes the graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.raw_extraction.schema import FormulaLink
from app.raw_extraction.column_utils import column_letter

_CELL_REF_RE = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)")
# Anchored single-cell / range parser for a sheet-local ref ("B2" or "B2:B742").
_RANGE_RE = re.compile(r"^(?P<c1>[A-Z]{1,3})(?P<r1>\d+)(?::(?P<c2>[A-Z]{1,3})(?P<r2>\d+))?$")


@dataclass
class FormulaGraph:
    """Directed graph of formula dependencies."""
    links: list[FormulaLink] = field(default_factory=list)
    input_cells: set[str] = field(default_factory=set)
    output_cells: set[str] = field(default_factory=set)

    def to_summary(self) -> dict:
        return {
            "total_links": len(self.links),
            "input_cells": sorted(self.input_cells)[:20],
            "output_cells": sorted(self.output_cells)[:20],
        }


def _col_to_num(col_str: str) -> int | None:
    """Excel column letters → 1-indexed int ('A' → 1, 'AA' → 27). None on junk."""
    n = 0
    for ch in col_str:
        if not ("A" <= ch <= "Z"):
            return None
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _split_qualified(ref: str) -> tuple[str, str]:
    """'Sheet!B2' -> ('Sheet', 'B2'). Splits on the LAST '!' so a stray '!'
    inside an (already-unquoted) sheet name doesn't break the address part."""
    i = ref.rfind("!")
    return (ref[:i], ref[i + 1:]) if i != -1 else ("", ref)


def _range_bounds(local_ref: str) -> tuple[int, int, int, int] | None:
    """'B2:B742' or 'B2' -> (min_row, min_col, max_row, max_col), 1-based."""
    m = _RANGE_RE.match(local_ref)
    if not m:
        return None
    c1 = _col_to_num(m.group("c1"))
    c2 = _col_to_num(m.group("c2")) if m.group("c2") else c1
    if c1 is None or c2 is None:
        return None
    r1 = int(m.group("r1"))
    r2 = int(m.group("r2")) if m.group("r2") else r1
    return (min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))


def build_formula_graph_from_precedents(
    precedents_by_cell: dict[str, list[str]],
    formula_addrs: set[str],
    populated_cells: set[str] | None = None,
    formula_strings: dict[str, str] | None = None,
) -> FormulaGraph:
    """Fold per-cell precedent RANGES into a dependency graph.

    ``precedents_by_cell`` maps a qualified formula cell ("Sheet!A1") to the
    resolved precedent RANGES it reads ("Sheet!B2:B742"). Links are kept at
    range granularity (compact). The input/output cell sets are computed by
    intersecting those ranges with ``populated_cells`` — the cells that
    actually contain something — so a 10,000-row or whole-column reference
    contributes only the cells that really exist, with no arbitrary cap and
    no enumeration of empty cells. ``formula_strings`` optionally records the
    formula text on each link.
    """
    graph = FormulaGraph()
    formula_strings = formula_strings or {}
    populated_cells = populated_cells or set()

    # Index populated cells by sheet → {(row, col)} for range intersection.
    populated_by_sheet: dict[str, set[tuple[int, int]]] = {}
    for qualified in populated_cells:
        sheet, addr = _split_qualified(qualified)
        m = _CELL_REF_RE.match(addr)
        if not m:
            continue
        col = _col_to_num(m.group(1))
        if col is not None:
            populated_by_sheet.setdefault(sheet, set()).add((int(m.group(2)), col))

    # Memoize range → concrete populated cells. Ranges repeat heavily across
    # formulas (a shared lookup column is referenced by thousands of cells), so
    # each distinct range is expanded at most once.
    expand_cache: dict[str, list[str]] = {}

    def expand(range_ref: str) -> list[str]:
        cached = expand_cache.get(range_ref)
        if cached is not None:
            return cached
        sheet, local = _split_qualified(range_ref)
        cells_in_sheet = populated_by_sheet.get(sheet)
        out: list[str] = []
        bounds = _range_bounds(local) if cells_in_sheet else None
        if bounds and cells_in_sheet:
            r1, c1, r2, c2 = bounds
            # Walk the sheet's populated cells (bounded by real data) and keep
            # those inside the range — cheaper than enumerating the range area.
            for (r, c) in cells_in_sheet:
                if r1 <= r <= r2 and c1 <= c <= c2:
                    out.append(f"{sheet}!{column_letter(c)}{r}")
        expand_cache[range_ref] = out
        return out

    referenced: set[str] = set()
    for target, ranges in precedents_by_cell.items():
        formula = formula_strings.get(target, "")
        for rng in ranges:
            graph.links.append(FormulaLink(source=rng, target=target, formula=formula))
            referenced.update(expand(rng))

    graph.input_cells = referenced - formula_addrs
    graph.output_cells = formula_addrs - referenced
    return graph
