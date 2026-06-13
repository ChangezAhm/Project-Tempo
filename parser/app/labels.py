"""Derive compact per-sheet row labels + column headers from parsed cells.

Build Step 3 stores these as JSON on template_sheets rather than persisting
every cell (which would be huge). Heuristic and intentionally simple — the
hierarchical metric model (Build Step 14) supersedes this later.
"""

from __future__ import annotations

from app.raw_extraction.column_utils import column_letter
from app.raw_extraction.schema import CellInfo, CellType

# Header band: column headers almost always live in the first dozen rows.
_HEADER_ROW_LIMIT = 12
_MAX_LABELS = 400


def derive_row_labels(cells: list[CellInfo]) -> dict[str, str]:
    """{row -> label text}: first string cell in columns A/B/C for each row."""
    best: dict[int, tuple[int, str]] = {}  # row -> (col, text)
    for c in cells:
        if c.col > 3 or c.cell_type != CellType.STRING:
            continue
        if not isinstance(c.value, str):
            continue
        text = c.value.strip()
        if not text:
            continue
        prev = best.get(c.row)
        if prev is None or c.col < prev[0]:
            best[c.row] = (c.col, text[:200])
    rows = sorted(best.items())[:_MAX_LABELS]
    return {str(r): text for r, (_, text) in rows}


def derive_column_headers(cells: list[CellInfo]) -> dict[str, str]:
    """{column letter -> header text}: topmost string cell per column in the header band."""
    best: dict[int, tuple[int, str]] = {}  # col -> (row, text)
    for c in cells:
        if c.row > _HEADER_ROW_LIMIT or c.cell_type != CellType.STRING:
            continue
        if not isinstance(c.value, str):
            continue
        text = c.value.strip()
        if not text:
            continue
        prev = best.get(c.col)
        if prev is None or c.row < prev[0]:
            best[c.col] = (c.row, text[:120])
    return {column_letter(col): text for col, (_, text) in sorted(best.items())}
