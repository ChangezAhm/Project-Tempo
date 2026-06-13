"""Section SIGNALS for the Layer-3 section LLM (not final sections).

Two signal sources:
  - regions: the contiguous blocks the vendored region_detector already found
    (sheet.regions, populated during parse).
  - title candidates: author-written header rows (bold / large / all-caps,
    standalone). Reimplemented from region_detector.find_section_titles but
    KEEPS THE ROW (the prototype's version returns only the text).
"""

from __future__ import annotations

from app.raw_extraction.schema import CellInfo, DetectedRegion
from app.structure.schema import Region, SectionTitleSignal


def regions_to_signals(regions: list[DetectedRegion]) -> list[Region]:
    return [
        Region(
            sheet_name=r.sheet_name,
            cell_range=r.cell_range,
            min_row=r.min_row,
            min_col=r.min_col,
            max_row=r.max_row,
            max_col=r.max_col,
            region_type=r.region_type,
            cell_count=r.cell_count,
            formula_count=r.formula_count,
            input_count=r.input_count,
            label_count=r.label_count,
        )
        for r in regions
    ]


def find_section_title_signals(cells: list[CellInfo], sheet_name: str) -> list[SectionTitleSignal]:
    """Author-written section title rows, with their row number.

    A title row: text in cols A/B/C, 3-80 chars, styled bold OR font >=12 OR
    short all-caps, and standalone (no numeric/formula/date data to the right).
    """
    if not cells:
        return []

    by_row: dict[int, list[CellInfo]] = {}
    for c in cells:
        by_row.setdefault(c.row, []).append(c)

    out: list[SectionTitleSignal] = []
    seen: set[str] = set()

    for c in sorted(cells, key=lambda x: (x.row, x.col)):
        if c.col > 3 or not isinstance(c.value, str):
            continue
        text = c.value.strip()
        if len(text) < 3 or len(text) > 80:
            continue

        style = c.style
        is_titleish = bool(style.bold) \
            or (style.font_size and style.font_size >= 12) \
            or (text.isupper() and len(text) <= 50)
        if not is_titleish:
            continue

        row_cells = by_row.get(c.row, [])
        has_data = any(
            other.col > c.col and other.cell_type.value in ("number", "formula", "date")
            for other in row_cells
        )
        if has_data:
            continue

        key = f"{c.row}:{text}"
        if key in seen:
            continue
        seen.add(key)
        out.append(SectionTitleSignal(sheet_name=sheet_name, row=c.row, text=text))

    return out
