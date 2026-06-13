"""Detect contiguous rectangular data regions in a sheet."""

from __future__ import annotations

from app.raw_extraction.column_utils import column_letter as get_column_letter

from app.raw_extraction.schema import CellInfo, CellRole, CellType, DetectedRegion


def find_section_titles(cells: list[CellInfo], regions: list[DetectedRegion]) -> list[str]:
    """Find author-written section title rows.

    A title row is a cell in col A/B/C whose text is:
      - 3-80 chars
      - styled bold OR larger font OR all-caps (short)
      - the row has no numeric data alongside the label (= it's standalone,
        not a metric row)

    We scan ALL cells (not just rows above regions) because in real templates
    the title often sits at the TOP of a region rather than above it.
    Returns titles in document (row) order, deduplicated.
    """
    if not cells:
        return []

    # Build row → cells mapping so we can check "is this row standalone?"
    by_row: dict[int, list[CellInfo]] = {}
    for c in cells:
        by_row.setdefault(c.row, []).append(c)

    titles: list[tuple[int, str]] = []
    seen: set[str] = set()

    for c in cells:
        if c.col > 3:
            continue
        if not isinstance(c.value, str):
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

        # Title rows are typically "standalone" — no numeric/formula data
        # in the same row. Skip rows that look like metric rows (label +
        # numbers) since those are metrics, not section headers.
        row_cells = by_row.get(c.row, [])
        has_data = any(
            other.col > c.col and other.cell_type.value in ("number", "formula", "date")
            for other in row_cells
        )
        if has_data:
            continue

        if text not in seen:
            titles.append((c.row, text))
            seen.add(text)

    titles.sort()
    return [t for _, t in titles]


def detect_regions(
    cells: list[CellInfo],
    sheet_name: str,
    max_gap: int = 2,
) -> list[DetectedRegion]:
    """Find contiguous rectangular blocks of non-empty cells.

    Groups cells into blocks separated by empty rows/columns.
    max_gap: number of empty rows/cols allowed within a single region.
    """
    if not cells:
        return []

    non_empty = [c for c in cells if c.cell_type != CellType.EMPTY]
    if not non_empty:
        return []

    # Build occupied row/col sets
    occupied_rows: set[int] = set()
    occupied_cols: set[int] = set()
    cell_map: dict[tuple[int, int], CellInfo] = {}

    for c in non_empty:
        occupied_rows.add(c.row)
        occupied_cols.add(c.col)
        cell_map[(c.row, c.col)] = c

    # Find row groups (blocks of rows separated by gaps > max_gap)
    row_groups = _group_indices(sorted(occupied_rows), max_gap)
    col_groups = _group_indices(sorted(occupied_cols), max_gap)

    regions: list[DetectedRegion] = []
    region_idx = 0

    for row_group in row_groups:
        for col_group in col_groups:
            min_row, max_row = row_group[0], row_group[-1]
            min_col, max_col = col_group[0], col_group[-1]

            # Count cells in this rectangle
            region_cells = [
                cell_map[(r, c)]
                for r in range(min_row, max_row + 1)
                for c in range(min_col, max_col + 1)
                if (r, c) in cell_map
            ]

            if len(region_cells) < 2:
                continue

            row_count = max_row - min_row + 1
            col_count = max_col - min_col + 1

            # Basic stats
            formula_count = sum(1 for c in region_cells if c.cell_type == CellType.FORMULA)
            input_count = sum(1 for c in region_cells if c.role == CellRole.INPUT)
            label_count = sum(1 for c in region_cells if c.role == CellRole.LABEL)

            # Determine region type
            if row_count <= 3 and any(c.role == CellRole.HEADER for c in region_cells):
                region_type = "header_block"
            elif formula_count > len(region_cells) * 0.3:
                region_type = "data_table"
            else:
                region_type = "data_block"

            cell_range = (
                f"{get_column_letter(min_col)}{min_row}:"
                f"{get_column_letter(max_col)}{max_row}"
            )

            regions.append(DetectedRegion(
                id=f"region_{region_idx:03d}",
                sheet_name=sheet_name,
                cell_range=cell_range,
                min_row=min_row,
                min_col=min_col,
                max_row=max_row,
                max_col=max_col,
                row_count=row_count,
                col_count=col_count,
                cell_count=len(region_cells),
                formula_count=formula_count,
                input_count=input_count,
                label_count=label_count,
                region_type=region_type,
            ))
            region_idx += 1

    return regions


def _group_indices(sorted_indices: list[int], max_gap: int) -> list[list[int]]:
    """Group sorted indices into runs, allowing gaps up to max_gap."""
    if not sorted_indices:
        return []
    groups: list[list[int]] = [[sorted_indices[0]]]
    for idx in sorted_indices[1:]:
        if idx - groups[-1][-1] <= max_gap:
            groups[-1].append(idx)
        else:
            groups.append([idx])
    return groups
