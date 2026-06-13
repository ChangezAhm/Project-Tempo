"""Extract row-level metric rows from a sheet's cells.

Ported from template-compiler-prototype/src/parsing/field_extractor.py.
Algorithm unchanged; output is the lean MetricRow (no section/classification).
A "metric row" is a labelled row: text in cols A/B/C + numeric/formula/date
data to the right.
"""

from __future__ import annotations

import re

from app.raw_extraction.column_utils import column_letter
from app.raw_extraction.schema import CellInfo, CellType
from app.structure.schema import MetricRow


def extract_metric_rows(cells: list[CellInfo], sheet_name: str) -> list[MetricRow]:
    cell_map: dict[tuple[int, int], CellInfo] = {(c.row, c.col): c for c in cells}

    # Leftmost text label in cols A/B/C per row.
    rows_with_labels: dict[int, CellInfo] = {}
    for c in cells:
        if c.col <= 3 and c.cell_type == CellType.STRING and c.value:
            label_text = str(c.value).strip()
            if len(label_text) >= 2 and not _is_noise(label_text):
                if c.row not in rows_with_labels or c.col < rows_with_labels[c.row].col:
                    rows_with_labels[c.row] = c

    out: list[MetricRow] = []
    for row_num, label_cell in sorted(rows_with_labels.items()):
        label_text = str(label_cell.value).strip()

        data_cols: list[int] = []
        sample_value = None
        number_format = None
        is_formula = False

        for col_num in range(label_cell.col + 1, 101):
            c = cell_map.get((row_num, col_num))
            if c is None:
                continue
            if c.cell_type in (CellType.NUMBER, CellType.FORMULA, CellType.DATE):
                data_cols.append(col_num)
                if sample_value is None:
                    sample_value = c.cached_value if c.cached_value is not None else c.value
                    number_format = c.style.number_format
                if not is_formula and c.cell_type == CellType.FORMULA:
                    is_formula = True

        if not data_cols:
            continue

        data_range = (
            f"{column_letter(data_cols[0])}{row_num}:{column_letter(data_cols[-1])}{row_num}"
            if data_cols
            else None
        )

        out.append(MetricRow(
            sheet_name=sheet_name,
            row=row_num,
            label_text=label_text,
            label_cell=f"{column_letter(label_cell.col)}{row_num}",
            label_col=label_cell.col,
            indent_level=int(getattr(label_cell.style, "indent_level", 0) or 0),
            data_cols=data_cols,
            data_range=data_range,
            is_formula=is_formula,
            is_bold=bool(label_cell.style.bold),
            is_strikethrough=bool(getattr(label_cell.style, "strikeout", False)),
            unit=_detect_unit(number_format, label_text),
            number_format=number_format,
            sample_value=_safe_value(sample_value),
            named_range=None,  # attached later if/when named-range matching is wired
        ))

    return out


def _detect_unit(number_format: str | None, label_text: str) -> str | None:
    if number_format:
        fmt = number_format.lower()
        if "%" in fmt:
            return "%"
        if "£" in fmt:
            return "£"
        if "$" in fmt:
            return "$"
        if "€" in fmt:
            return "€"
        if '"x"' in fmt or fmt.endswith("x") or "0.0x" in fmt:
            return "x"

    label_lower = label_text.lower()
    if "(£m)" in label_lower or "£m" in label_lower or "(gbp m)" in label_lower:
        return "£m"
    if "($m)" in label_lower or "$m" in label_lower or "(usd m)" in label_lower:
        return "$m"
    if "(€m)" in label_lower or "€m" in label_lower or "(eur m)" in label_lower:
        return "€m"
    if "(%)" in label_lower or label_lower.endswith(" %"):
        return "%"
    if "(x)" in label_lower or label_lower.endswith(" x") or label_lower.endswith("(x)"):
        return "x"
    if "(#)" in label_lower or "headcount" in label_lower or "fte" in label_lower:
        return "#"
    return None


_NOISE_PATTERNS = re.compile(
    r"^(total|sub-?total|check|blank|page|section|note|source|ref)$",
    re.IGNORECASE,
)


def _is_noise(text: str) -> bool:
    if len(text) <= 1:
        return True
    if text.startswith("="):
        return True
    if _NOISE_PATTERNS.match(text.strip()):
        return True
    return False


def _safe_value(val):
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    return str(val)
