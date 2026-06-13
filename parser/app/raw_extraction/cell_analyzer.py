"""Aspose.Cells-backed cell analysis: type, style, role inference."""

from __future__ import annotations

import datetime

from aspose.cells import Cell, CellValueType

from app.raw_extraction.schema import CellInfo, CellRole, CellStyle, CellType
from app.raw_extraction.column_utils import column_letter


# Curated set of fill RGB triplets that templates conventionally use for input
# cells. We match against the RGB part only (alpha stripped, lowercased) — a
# substring check against the raw ARGB string is unsafe because "ffff" matches
# white (FFFFFFFF) just as well as yellow tints.
_INPUT_FILL_HEXES = frozenset({
    "ffff00",  # pure yellow
    "ffff99",  # light yellow
    "ffffcc",  # very light yellow
    "ffffe0",  # light yellow
    "fff2cc",  # pale yellow (Excel theme)
    "fce4d6",  # peach (Excel theme)
    "ffd966",  # gold
    "ffe699",  # light gold
    "ddebf7",  # very light blue (Excel theme)
    "bdd7ee",  # light blue (Excel theme)
    "d9e1f2",  # light blue tint
})


def is_input_fill(fill_color: str | None) -> bool:
    """True if a fill color matches a conventional input-cell color."""
    if not fill_color:
        return False
    if fill_color.startswith(("indexed:", "theme:")):
        return False
    rgb = fill_color.lower()
    if len(rgb) == 8:
        rgb = rgb[2:]
    return rgb in _INPUT_FILL_HEXES


def _argb_hex(color) -> str | None:
    """Convert an Aspose Color → 8-char ARGB hex, or None if empty/automatic."""
    if color is None:
        return None
    try:
        argb = color.to_argb()
    except Exception:
        return None
    # Aspose returns -1 (or another negative sentinel) for "automatic"/unset.
    if argb is None or argb == -1:
        return None
    argb_u = argb & 0xFFFFFFFF
    if argb_u == 0:
        # Fully transparent / no fill set.
        return None
    return f"{argb_u:08X}"


def _extract_style(cell: Cell) -> CellStyle:
    """Pull style fields off an Aspose cell."""
    try:
        s = cell.get_style()
    except Exception:
        return CellStyle()

    font = s.font
    font_color = _argb_hex(font.color) if font else None
    fill_color = _argb_hex(s.foreground_color)

    # Aspose uses 'General' for unformatted cells; treat as None to match the
    # downstream code that looks for currency / percentage markers in the
    # format string.
    custom = ""
    try:
        custom = s.custom or ""
    except Exception:
        custom = ""
    number_format = custom if custom and custom != "General" else None

    # Borders — true if any of the four sides has a non-NONE style.
    has_border = False
    try:
        borders = s.borders
        if borders is not None:
            for side in ("top_border", "bottom_border", "left_border", "right_border"):
                b = getattr(borders, side, None)
                if b is not None and getattr(b, "line_style", 0):
                    has_border = True
                    break
    except Exception:
        pass

    # Alignment enums → string (best-effort, fall back to None)
    h_align = v_align = None
    try:
        h_align = str(s.horizontal_alignment).rsplit(".", 1)[-1].lower()
    except Exception:
        pass
    try:
        v_align = str(s.vertical_alignment).rsplit(".", 1)[-1].lower()
    except Exception:
        pass

    # Cell-level locked flag — meaningful only when the sheet is protected.
    # Excel's default is True; authors UNLOCK exactly the cells the user is
    # meant to fill in, so this is the most authoritative input signal.
    is_locked = True
    try:
        is_locked = bool(s.is_locked)
    except Exception:
        pass

    # Strikethrough often marks deprecated rows we shouldn't tell users to fill.
    # Aspose Python uses `is_strikeout`; some versions also expose `strikeout`.
    strikeout = False
    if font is not None:
        for attr in ("is_strikeout", "strikeout", "strike_through"):
            try:
                v = getattr(font, attr, None)
                if v is True:
                    strikeout = True
                    break
            except Exception:
                pass

    # Indent level — Excel stores this as int 0..15. Captures the parent/child
    # hierarchy you see in any P&L (Revenue > Product/Service > Total Revenue).
    indent_level = 0
    try:
        indent_level = int(getattr(s, "indent_level", 0) or 0)
    except Exception:
        pass

    # Rotation in degrees. Vertical text in headers is common.
    rotation_angle = 0.0
    try:
        rotation_angle = float(getattr(s, "rotation_angle", 0.0) or 0.0)
    except Exception:
        pass

    return CellStyle(
        bold=bool(font and font.is_bold),
        italic=bool(font and font.is_italic),
        strikeout=strikeout,
        font_size=float(font.size) if font and font.size else None,
        font_color=font_color,
        fill_color=fill_color,
        number_format=number_format,
        h_alignment=h_align,
        v_alignment=v_align,
        has_border=has_border,
        is_locked=is_locked,
        indent_level=indent_level,
        rotation_angle=rotation_angle,
    )


def _detect_cell_type(cell: Cell) -> CellType:
    """Map Aspose CellValueType → our CellType. Formulas win regardless of result type."""
    if cell.is_formula:
        return CellType.FORMULA
    t = cell.type
    if t == CellValueType.IS_NULL or t == CellValueType.IS_UNKNOWN:
        return CellType.EMPTY
    if t == CellValueType.IS_NUMERIC:
        return CellType.NUMBER
    if t == CellValueType.IS_DATE_TIME:
        return CellType.DATE
    if t == CellValueType.IS_BOOL:
        return CellType.BOOLEAN
    if t == CellValueType.IS_ERROR:
        return CellType.ERROR
    if t == CellValueType.IS_STRING:
        return CellType.STRING
    # Fallback: treat any unknown bucket with a non-None value as string.
    return CellType.STRING if cell.value is not None else CellType.EMPTY


def _serialize_value(value):
    """Coerce a cell value to a JSON-safe Python type."""
    if value is None:
        return None
    if isinstance(value, bool):  # check before int — bool is an int subclass
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return str(value)


def infer_role(cell_type: CellType, style: CellStyle, col: int, formula: str | None) -> CellRole:
    """Soft role classification based on type, style, and column position."""
    if cell_type == CellType.EMPTY:
        return CellRole.EMPTY
    if cell_type == CellType.FORMULA:
        return CellRole.FORMULA

    if style.bold and col <= 3:
        return CellRole.HEADER
    if style.font_size and style.font_size >= 12 and col <= 3:
        return CellRole.HEADER

    if cell_type in (CellType.NUMBER, CellType.STRING) and is_input_fill(style.fill_color):
        return CellRole.INPUT

    if cell_type == CellType.STRING and col <= 2:
        return CellRole.LABEL

    return CellRole.DATA


def analyze_cell(cell: Cell, row_1based: int, col_1based: int) -> CellInfo:
    """Build a CellInfo from an Aspose Cell."""
    address = f"{column_letter(col_1based)}{row_1based}"
    style = _extract_style(cell)
    cell_type = _detect_cell_type(cell)

    formula = None
    raw_value = cell.value
    cached_value = None

    if cell_type == CellType.FORMULA:
        f = cell.formula or ""
        formula = f if f.startswith("=") else f"={f}" if f else "="
        cached_value = _serialize_value(raw_value)
        display_value = formula
    else:
        display_value = _serialize_value(raw_value)

    role = infer_role(cell_type, style, col_1based, formula)

    return CellInfo(
        address=address,
        row=row_1based,
        col=col_1based,
        value=display_value,
        cached_value=cached_value,
        formula=formula,
        cell_type=cell_type,
        role=role,
        style=style,
    )
