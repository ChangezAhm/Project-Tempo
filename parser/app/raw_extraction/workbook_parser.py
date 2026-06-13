"""Aspose.Cells-backed workbook parser.

Single-pass: Aspose gives us formula + cached value + dependency graph
directly from one workbook load, so the prior two-pass openpyxl dance is
gone. The formula graph is built from ``cell.get_precedents()`` rather
than parsing formula strings ourselves, which sidesteps the string-literal
false-positive class entirely.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from aspose.cells import Workbook

from app.raw_extraction.schema import (
    CellComment,
    CellInfo,
    CellType,
    ChartCaption,
    ConditionalFormatRule,
    DataValidationRule,
    DetectedRegion,
    Hyperlink,
    MergedRange,
    NamedRange,
    PageHeaderFooter,
    PictureNote,
    SheetCellData,
    TextBoxNote,
    WorkbookMetadata,
)
from app.raw_extraction.cell_analyzer import analyze_cell, is_input_fill
from app.raw_extraction.column_utils import column_letter
from app.raw_extraction.formula_mapper import FormulaGraph, build_formula_graph_from_precedents
from app.raw_extraction.region_detector import detect_regions

logger = logging.getLogger(__name__)

# Safety caps — these are NOT the analysis boundary. The per-sheet used
# range comes from Aspose (``cells.max_data_row`` / ``cells.max_data_column``,
# which is what Excel's Ctrl+End maps to) and is respected directly. These
# constants kick in only when a sheet exceeds them — a backstop against a
# pathological workbook with a stray value out near Excel's physical limits
# (1,048,576 rows × 16,384 cols). They are deliberately generous: real PE
# templates can be very wide (dynamic-array / spill formulas reach 2,000+
# cols — e.g. IPV's Quarterly_Output at ~2,021), and clipping legitimate data
# is worse than the cost of a slightly larger scan. Since cell iteration is
# sparse (only non-empty cells), raising these is cheap.
MAX_ROWS_PER_SHEET = 100000
MAX_COLS_PER_SHEET = 4096


def _detect_vba(file_path: Path) -> bool:
    """Cheap zip-archive peek for vbaProject.bin — doesn't load any macro bytes."""
    try:
        with zipfile.ZipFile(file_path) as zf:
            return any("vbaProject.bin" in name for name in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


class ParsedSheet:
    """Per-sheet bundle of parsed data."""

    def __init__(self, name: str, index: int, is_hidden: bool = False):
        self.name = name
        self.index = index
        self.is_hidden = is_hidden
        self.cells: list[CellInfo] = []
        self.merged_ranges: list[MergedRange] = []
        self.regions: list[DetectedRegion] = []
        self.comments: list[CellComment] = []
        self.data_validations: list[DataValidationRule] = []
        self.conditional_formats: list[ConditionalFormatRule] = []
        self.text_box_notes: list[TextBoxNote] = []
        self.page_headers_footers: list[PageHeaderFooter] = []
        self.chart_captions: list[ChartCaption] = []
        self.picture_notes: list[PictureNote] = []
        self.hyperlinks: list[Hyperlink] = []
        self.is_protected: bool = False
        self.tab_color: str | None = None
        self.frozen_rows: int = 0
        self.frozen_cols: int = 0
        self.print_area: str | None = None
        self.narrow_columns: list[int] = []
        self.row_group_levels: dict[int, int] = {}  # 1-based row -> Excel outline/group level
        self.row_count: int = 0
        self.col_count: int = 0
        # Aspose-detected used range (Excel's Ctrl+End equivalent) — recorded
        # separately from row_count/col_count so the blueprint can be honest
        # when the safety cap actually trimmed something.
        self.used_max_row: int = 0
        self.used_max_col: int = 0
        self.was_truncated: bool = False

    def to_cell_data(self) -> SheetCellData:
        return SheetCellData(
            cells=self.cells,
            merged_ranges=self.merged_ranges,
            row_count=self.row_count,
            col_count=self.col_count,
        )


class ParsedWorkbook:
    """Top-level bundle of parsed data."""

    def __init__(self):
        self.metadata = WorkbookMetadata(filename="")
        self.sheets: list[ParsedSheet] = []
        self.named_ranges: list[NamedRange] = []
        self.formula_graph: FormulaGraph = FormulaGraph()

    def get_sheet(self, name: str) -> ParsedSheet | None:
        for s in self.sheets:
            if s.name == name:
                return s
        return None


def _referred_area_to_range(area, sheet_name: str) -> str:
    """Aspose ReferredArea → a single qualified range string.

    Stores the reference LOSSLESSLY as the range Aspose already resolved
    (e.g. ``Data!B2:B742``, or ``Data!A1`` for a single cell) instead of
    expanding it to individual cells. No cap is needed: a formula references
    only a handful of distinct areas, however large each one is. The concrete
    dependency edges are recovered later by intersecting these ranges with the
    cells that actually exist (see ``build_formula_graph_from_precedents``), so
    a 10,000-row or whole-column reference never explodes the graph.
    """
    src_sheet = area.sheet_name or sheet_name
    sr, er = area.start_row, area.end_row
    sc, ec = area.start_column, area.end_column
    top_left = f"{column_letter(sc + 1)}{sr + 1}"
    if sr == er and sc == ec:
        return f"{src_sheet}!{top_left}"
    return f"{src_sheet}!{top_left}:{column_letter(ec + 1)}{er + 1}"


def _extract_comments(ws, sheet_name: str) -> list[CellComment]:
    out: list[CellComment] = []
    try:
        comments = ws.comments
    except Exception:
        return out
    for c in comments:
        try:
            addr = f"{column_letter(c.column + 1)}{c.row + 1}"
            out.append(CellComment(
                cell_address=addr,
                sheet_name=sheet_name,
                author=c.author or "",
                text=(c.note or "")[:2000],
            ))
        except Exception:
            continue
    return out


# Aspose ValidationType enum (Python binding stringifies as int sometimes,
# enum name other times). Map to canonical lowercase names.
_VALIDATION_TYPE_INT_MAP = {
    0: "none",
    1: "whole",
    2: "decimal",
    3: "list",
    4: "date",
    5: "time",
    6: "textlength",
    7: "custom",
    8: "any",
}


def _normalize_validation_type(raw_type) -> str:
    """Map an Aspose ValidationType (int or enum) to a canonical lowercase string."""
    if raw_type is None:
        return ""
    # Try int first
    try:
        i = int(raw_type)
        if i in _VALIDATION_TYPE_INT_MAP:
            return _VALIDATION_TYPE_INT_MAP[i]
    except (TypeError, ValueError):
        pass
    # Fall back to enum-name stringification
    s = str(raw_type).rsplit(".", 1)[-1].lower().strip()
    if not s:
        return ""
    if s.isdigit():
        return _VALIDATION_TYPE_INT_MAP.get(int(s), "")
    # Normalize common spellings
    if s in ("textlength", "text_length"):
        return "textlength"
    return s


def _extract_validations(ws, sheet_name: str) -> list[DataValidationRule]:
    out: list[DataValidationRule] = []
    try:
        validations = ws.validations
    except Exception:
        return out
    for v in validations:
        try:
            # Aspose can attach a validation to multiple cell areas.
            ranges: list[str] = []
            try:
                for area in v.areas:
                    ranges.append(
                        f"{column_letter(area.start_column + 1)}{area.start_row + 1}:"
                        f"{column_letter(area.end_column + 1)}{area.end_row + 1}"
                    )
            except Exception:
                pass
            cell_range = ",".join(ranges)
            out.append(DataValidationRule(
                sheet_name=sheet_name,
                cell_range=cell_range,
                validation_type=_normalize_validation_type(v.type),
                formula1=str(v.formula1 or ""),
                formula2=str(v.formula2 or ""),
                operator=str(v.operator).rsplit(".", 1)[-1].lower() if v.operator is not None else "",
                allow_blank=bool(getattr(v, "ignore_blank", True)),
                prompt_title=str(getattr(v, "input_title", "") or ""),
                prompt_message=str(getattr(v, "input_message", "") or ""),
                error_title=str(getattr(v, "alert_style", "") or ""),
                error_message=str(getattr(v, "error_message", "") or ""),
            ))
        except Exception as e:
            logger.debug(f"validation extract failed: {e}")
    return out


# Header/footer format codes (&L, &C, &R, &P, &D, &T, &B, &I, &U,
# &"font,style", &<font-size>, &K<color>, etc.) — strip to leave plain text.
_HF_CODE_RE = re.compile(r'&(?:"[^"]*"|[A-Za-z0-9]|&)')


def _strip_excel_format_codes(s: str) -> str:
    if not s:
        return ""
    return _HF_CODE_RE.sub("", s)


def _anchor_cell(shape, default_sheet: str | None = None) -> str | None:
    """Top-left anchor cell address of an Aspose shape (Sheet-omitted)."""
    try:
        row = shape.upper_left_row
        col = shape.upper_left_column
    except Exception:
        return None
    if row is None or col is None:
        return None
    try:
        return f"{column_letter(int(col) + 1)}{int(row) + 1}"
    except Exception:
        return None


def _shape_bounding_box(shape) -> tuple[int, int, int, int] | None:
    """Return (upper_row, upper_col, lower_row, lower_col) in 1-indexed coords.

    Aspose exposes both ``upper_left_*`` and ``lower_right_*`` for any shape
    that anchors to the grid. None if either corner is unavailable.
    """
    try:
        ur = int(shape.upper_left_row) + 1
        uc = int(shape.upper_left_column) + 1
        lr = int(shape.lower_right_row) + 1
        lc = int(shape.lower_right_column) + 1
    except Exception:
        return None
    if ur > lr or uc > lc:
        return None
    return (ur, uc, lr, lc)


def _coverage_range_str(ur: int, uc: int, lr: int, lc: int) -> str:
    """Format a bounding box as 'L4' (1×1) or 'L4:O8' (multi-cell)."""
    top_left = f"{column_letter(uc)}{ur}"
    if ur == lr and uc == lc:
        return top_left
    return f"{top_left}:{column_letter(lc)}{lr}"


def _find_nearby_labels(
    cells,
    ur: int,
    uc: int,
    lr: int,
    lc: int,
    max_labels: int = 5,
) -> list[str]:
    """Find label-like cells near a shape's bounding box.

    Priority:
      (1) Labels in cols A/B/C at row range overlapping the box — for a
          right-side text box annotating a section, these are the actual
          metrics being annotated.
      (2) Any other non-empty text cell within ±2 rows / ±8 cols of the box
          edge, ranked by row-weighted Manhattan distance.

    Returns formatted strings like ``"A12: Revenue"`` (label cell address +
    text), capped at ``max_labels`` entries. Order is primary-first.
    """
    if not cells:
        return []

    primary: list[tuple[int, str, str]] = []      # (row, address, text)
    secondary: list[tuple[float, str, str]] = []  # (distance, address, text)
    seen_addrs: set[str] = set()

    # Window for the secondary pass — generous on the horizontal because
    # template authors typically float text boxes to the right of the data.
    row_min = max(1, ur - 2)
    row_max = lr + 2
    col_min = max(1, uc - 8)
    col_max = lc + 8

    for c in cells:
        if not isinstance(c.value, str):
            continue
        text = c.value.strip()
        if len(text) < 2 or len(text) > 120:
            continue
        # Skip cells INSIDE the box itself (the box's text isn't its context).
        if ur <= c.row <= lr and uc <= c.col <= lc:
            continue

        # Priority 1: col A/B/C label, row overlaps box vertical span.
        if c.col <= 3 and ur <= c.row <= lr:
            if c.address not in seen_addrs:
                primary.append((c.row, c.address, text[:80]))
                seen_addrs.add(c.address)
            continue

        # Priority 2: any text cell inside the search window.
        if not (row_min <= c.row <= row_max and col_min <= c.col <= col_max):
            continue
        dr = max(0, max(ur - c.row, c.row - lr))
        dc = max(0, max(uc - c.col, c.col - lc))
        distance = dr * 2 + dc  # rows weighted; templates are row-oriented
        if c.address not in seen_addrs:
            secondary.append((distance, c.address, text[:80]))
            seen_addrs.add(c.address)

    primary.sort()
    secondary.sort()

    out: list[str] = []
    for _, addr, text in primary[:max_labels]:
        out.append(f"{addr}: {text}")
    remaining = max_labels - len(out)
    if remaining > 0:
        for _, addr, text in secondary[:remaining]:
            out.append(f"{addr}: {text}")
    return out


def _shape_type_tag(shape) -> str:
    """Coarse classification of an Aspose shape's MSO type.

    The Python binding can stringify ``shape.type`` as either a qualified enum
    name (``MsoDrawingType.TEXT_BOX``) or a bare integer code, depending on
    version. Treat bare integers / blanks as ``text_box`` since we only reach
    this code path for shapes we've already confirmed carry non-empty text —
    a sensible default that's correct for the overwhelming majority of cases.
    """
    try:
        t = str(shape.type).rsplit(".", 1)[-1].lower().strip()
    except Exception:
        return "text_box"
    if not t or t.isdigit():
        return "text_box"
    if "textbox" in t or ("text" in t and "box" in t):
        return "text_box"
    if "callout" in t:
        return "callout"
    if "banner" in t:
        return "banner"
    if "arrow" in t:
        return "arrow"
    if "group" in t:
        return "group"
    if "rect" in t:
        return "rectangle"
    return t or "text_box"


def _shape_has_chart(shape) -> bool:
    """True if this shape is a chart object — we extract those via ws.charts
    so we skip them in the generic text-box pass to avoid double-counting."""
    try:
        if getattr(shape, "has_chart", False):
            return True
    except Exception:
        pass
    try:
        return getattr(shape, "chart", None) is not None
    except Exception:
        return False


def _extract_text_boxes(
    ws,
    sheet_name: str,
    cells: list[CellInfo] | None = None,
) -> list[TextBoxNote]:
    """Walk every shape on the sheet and capture any visible text content.

    Covers text boxes, callouts, banners, labelled arrows, rectangles with
    text, and any other shape carrying a non-empty .text — exactly the
    'guidance on the sides' a template author writes outside the grid.

    When ``cells`` is provided, each note is enriched with:
      - ``coverage_range`` — the full rectangular footprint (e.g. ``L4:O8``)
        instead of just the top-left anchor.
      - ``nearby_labels`` — label-like cells the box appears to annotate,
        so a downstream LLM knows what section the guidance is attached to.
    """
    out: list[TextBoxNote] = []
    try:
        shapes = ws.shapes
    except Exception:
        return out
    if shapes is None:
        return out

    for shape in shapes:
        try:
            if _shape_has_chart(shape):
                # Chart titles/axes go through _extract_chart_captions.
                continue
            text = ""
            try:
                text = (shape.text or "").strip()
            except Exception:
                text = ""
            if not text:
                # Some shape kinds expose text via text_body / html_text only.
                for attr in ("text_body", "html_text"):
                    try:
                        v = getattr(shape, attr, None)
                        if v:
                            t = getattr(v, "text", None) or str(v)
                            if t and t.strip():
                                text = t.strip()
                                break
                    except Exception:
                        continue
            if not text:
                continue

            anchor = _anchor_cell(shape, sheet_name)
            bbox = _shape_bounding_box(shape)
            coverage = _coverage_range_str(*bbox) if bbox else anchor
            nearby = _find_nearby_labels(cells, *bbox) if (bbox and cells) else []

            out.append(TextBoxNote(
                sheet_name=sheet_name,
                name=str(getattr(shape, "name", "") or ""),
                text=text[:2000],
                anchor_cell=anchor,
                coverage_range=coverage,
                nearby_labels=nearby,
                shape_type=_shape_type_tag(shape),
            ))
        except Exception as e:
            logger.debug(f"text-box extract failed for '{sheet_name}': {e}")
            continue
    return out


def _extract_headers_footers(ws, sheet_name: str) -> list[PageHeaderFooter]:
    """Pull page header/footer text for each section (L/C/R)."""
    out: list[PageHeaderFooter] = []
    try:
        ps = ws.page_setup
    except Exception:
        return out
    if ps is None:
        return out

    sections = [("left", 0), ("center", 1), ("right", 2)]
    for kind, getter_name in (("header", "get_header"), ("footer", "get_footer")):
        getter = getattr(ps, getter_name, None)
        if getter is None:
            continue
        for label, idx in sections:
            raw = ""
            try:
                raw = getter(idx) or ""
            except Exception:
                continue
            cleaned = _strip_excel_format_codes(str(raw)).strip()
            if not cleaned:
                continue
            out.append(PageHeaderFooter(
                sheet_name=sheet_name,
                kind=kind,
                section=label,
                text=cleaned[:1000],
            ))
    return out


def _safe_title_text(title_obj) -> str:
    """Resolve a chart/axis title's text across binding-version differences."""
    if title_obj is None:
        return ""
    for attr in ("text", "value"):
        try:
            v = getattr(title_obj, attr, None)
            if v:
                return str(v).strip()
        except Exception:
            continue
    return ""


def _extract_chart_captions(ws, sheet_name: str) -> list[ChartCaption]:
    """Chart name, title, and primary axis labels for every chart on a sheet."""
    out: list[ChartCaption] = []
    try:
        charts = ws.charts
    except Exception:
        return out
    if charts is None:
        return out

    for ch in charts:
        try:
            name = str(getattr(ch, "name", "") or "")
            title = ""
            try:
                title = _safe_title_text(getattr(ch, "title", None))
            except Exception:
                pass

            cat_title = val_title = ""
            try:
                cat = getattr(ch, "category_axis", None)
                if cat is not None:
                    cat_title = _safe_title_text(getattr(cat, "title", None))
            except Exception:
                pass
            try:
                val = getattr(ch, "value_axis", None)
                if val is not None:
                    val_title = _safe_title_text(getattr(val, "title", None))
            except Exception:
                pass

            if not (name or title or cat_title or val_title):
                continue

            anchor = None
            try:
                anchor = _anchor_cell(ch.chart_object, sheet_name)
            except Exception:
                anchor = _anchor_cell(ch, sheet_name)

            out.append(ChartCaption(
                sheet_name=sheet_name,
                chart_name=name,
                chart_title=title,
                category_axis_title=cat_title,
                value_axis_title=val_title,
                anchor_cell=anchor,
            ))
        except Exception as e:
            logger.debug(f"chart caption extract failed for '{sheet_name}': {e}")
            continue
    return out


def _extract_pictures(ws, sheet_name: str) -> list[PictureNote]:
    """Picture alt-text / title — accessibility fields often carry guidance."""
    out: list[PictureNote] = []
    try:
        pics = ws.pictures
    except Exception:
        return out
    if pics is None:
        return out

    for p in pics:
        try:
            name = str(getattr(p, "name", "") or "")
            alt = str(getattr(p, "alternative_text", "") or "")
            title = str(getattr(p, "title", "") or "")
            if not (alt.strip() or title.strip()):
                continue
            out.append(PictureNote(
                sheet_name=sheet_name,
                name=name,
                alternative_text=alt[:1000],
                title=title[:500],
                anchor_cell=_anchor_cell(p, sheet_name),
            ))
        except Exception as e:
            logger.debug(f"picture extract failed for '{sheet_name}': {e}")
            continue
    return out


def _extract_document_properties(wb) -> dict:
    """Pull the author/title/company/etc. fields out of File > Properties.

    These are stored on ``wb.built_in_document_properties`` as a name-keyed
    collection in Aspose. We try a handful of well-known fields and fall
    back silently when one's missing.
    """
    out = {
        "title": "", "author": "", "company": "", "subject": "",
        "keywords": "", "comments": "", "last_modified_by": "",
        "created_date": None, "modified_date": None,
    }
    try:
        bdp = wb.built_in_document_properties
    except Exception:
        return out
    if bdp is None:
        return out

    # Aspose exposes both attribute-style and lookup-style access; use attrs.
    field_map = {
        "title": "title",
        "author": "author",
        "company": "company",
        "subject": "subject",
        "keywords": "keywords",
        "comments": "comments",
        "last_modified_by": "last_saved_by",
    }
    for our_key, aspose_attr in field_map.items():
        try:
            v = getattr(bdp, aspose_attr, None)
            out[our_key] = str(v) if v else ""
        except Exception:
            pass
    for our_key, aspose_attr in (("created_date", "created_time"),
                                  ("modified_date", "last_saved_time")):
        try:
            v = getattr(bdp, aspose_attr, None)
            if v is not None:
                # Aspose returns a DateTime; ISO-format via str().
                out[our_key] = str(v)
        except Exception:
            pass
    return out


def _extract_tab_color(ws) -> str | None:
    """Sheet tab color as 6-char hex RGB, or None if no custom color set."""
    try:
        color = ws.tab_color
    except Exception:
        return None
    if color is None:
        return None
    hex_argb = _argb_hex_from_color(color)
    if not hex_argb:
        return None
    # Strip the alpha byte for a clean 6-char RGB string.
    return hex_argb[2:] if len(hex_argb) == 8 else hex_argb


def _argb_hex_from_color(color) -> str | None:
    """Convert an Aspose Color to ARGB hex, mirroring cell_analyzer._argb_hex."""
    try:
        argb = color.to_argb()
    except Exception:
        return None
    if argb is None or argb == -1:
        return None
    argb_u = argb & 0xFFFFFFFF
    if argb_u == 0:
        return None
    return f"{argb_u:08X}"


def _extract_frozen_panes(ws) -> tuple[int, int]:
    """Number of frozen rows / frozen columns (0 if no panes frozen).

    Aspose's Python binding stores the freeze state in several places that
    vary by version and by whether the file was saved by Excel vs Aspose
    eval mode. Probe in order of authority:

      1. ``ws.pane_state == FROZEN`` (2 in the PaneStateType enum) — definitive
         "panes are frozen" signal. Use ``get_panes().first_visible_*`` for counts.
      2. Plain ``ws.split_first_row/column`` / ``freezed_rows/columns`` attrs.
      3. Fallback: ``ws.first_visible_row/column`` (only set on real files).
    """
    # 1. pane_state + get_panes()
    try:
        ps = int(getattr(ws, "pane_state", -1))
        # PaneStateType: 0=SPLIT, 1=SPLIT_FROZEN, 2=FROZEN, 3=NORMAL
        if ps in (1, 2):  # any frozen variant
            panes = ws.get_panes()
            r = int(getattr(panes, "first_visible_row_of_bottom_pane", 0) or 0)
            c = int(getattr(panes, "first_visible_column_of_right_pane", 0) or 0)
            if r > 0 or c > 0:
                return (max(0, r), max(0, c))
    except Exception:
        pass

    # 2. Probe known attribute names
    rows = cols = 0
    for attr in ("split_first_row", "split_first_visible_row",
                 "freezed_rows", "frozen_rows"):
        try:
            v = getattr(ws, attr, None)
            if v is not None and int(v) > 0:
                rows = int(v)
                break
        except Exception:
            continue
    for attr in ("split_first_column", "split_first_visible_column",
                 "freezed_columns", "frozen_columns"):
        try:
            v = getattr(ws, attr, None)
            if v is not None and int(v) > 0:
                cols = int(v)
                break
        except Exception:
            continue

    # 3. Fallback to first_visible_*
    if rows == 0:
        try:
            rows = int(getattr(ws, "first_visible_row", 0) or 0)
        except Exception:
            pass
    if cols == 0:
        try:
            cols = int(getattr(ws, "first_visible_column", 0) or 0)
        except Exception:
            pass
    return (max(0, rows), max(0, cols))


def _extract_print_area(ws) -> str | None:
    """Author-designated print area, e.g. 'A1:N50', or None if not set."""
    try:
        ps = ws.page_setup
        pa = ps.print_area
        if pa and str(pa).strip():
            return str(pa).strip()
    except Exception:
        pass
    return None


# Excel default column width is ~8.43 chars; templates use narrow cols
# (< 3 chars) as visual spacers between sections.
_NARROW_COL_THRESHOLD = 3.0


def _extract_narrow_columns(ws, max_col: int) -> list[int]:
    """Columns narrower than the threshold (= likely spacers, not data)."""
    out: list[int] = []
    for c in range(max_col):
        try:
            width = float(ws.cells.get_column_width(c))
        except Exception:
            continue
        if 0 < width < _NARROW_COL_THRESHOLD:
            out.append(c + 1)  # 1-indexed
    return out


def _extract_hyperlinks(ws, sheet_name: str) -> list[Hyperlink]:
    """Cell hyperlinks — both internal navigation and external URLs."""
    out: list[Hyperlink] = []
    try:
        hls = ws.hyperlinks
    except Exception:
        return out
    if hls is None:
        return out

    for h in hls:
        try:
            area = h.area
            addr = f"{column_letter(area.start_column + 1)}{area.start_row + 1}"
            address = str(getattr(h, "address", "") or "")
            display = str(getattr(h, "text_to_display", "") or "")
            tooltip = str(getattr(h, "screen_tip", "") or "")

            is_internal = False
            target_sheet = None
            target_cell = None
            if address:
                # Internal Excel references look like 'Sheet!A1' or '#Sheet!A1'.
                cleaned = address.lstrip("#")
                if "!" in cleaned and not cleaned.startswith(("http://", "https://", "file:", "mailto:")):
                    is_internal = True
                    parts = cleaned.split("!", 1)
                    target_sheet = parts[0].strip("'")
                    target_cell = parts[1]

            out.append(Hyperlink(
                sheet_name=sheet_name,
                cell_address=addr,
                display_text=display[:200],
                url=address[:500],
                is_internal=is_internal,
                target_sheet=target_sheet,
                target_cell=target_cell,
                tooltip=tooltip[:200],
            ))
        except Exception as e:
            logger.debug(f"hyperlink extract failed for '{sheet_name}': {e}")
            continue
    return out


def _resolve_validation_list_values(formula1: str, all_sheets: dict) -> list[str]:
    """Resolve a list-validation's allowed values.

    Two shapes to handle:
      (1) Literal: ``"Air,Sea,Land"`` (sometimes with leading ``=``).
      (2) Range reference: ``=Lookups!$A$2:$A$15`` or ``Lookups!A2:A15`` —
          look up the actual cell values.

    Named-range references (``=MyList``) aren't resolved here; the caller
    can do a second pass with the workbook's named ranges if needed.
    """
    if not formula1:
        return []
    s = formula1.strip().lstrip("=").strip()
    if not s:
        return []

    # Range-shaped: contains "!" or matches a plain Excel range like A1:A10
    range_match = re.match(
        r"^(?:'?(?P<sheet>[^'!]+)'?!)?\$?(?P<c1>[A-Z]{1,3})\$?(?P<r1>\d+)(?::\$?(?P<c2>[A-Z]{1,3})\$?(?P<r2>\d+))?$",
        s,
    )
    if range_match:
        sheet_name = range_match.group("sheet")
        c1 = _col_to_num(range_match.group("c1"))
        r1 = int(range_match.group("r1"))
        c2 = _col_to_num(range_match.group("c2") or range_match.group("c1"))
        r2 = int(range_match.group("r2") or range_match.group("r1"))
        if c1 is None or c2 is None:
            return []
        if c1 > c2:
            c1, c2 = c2, c1
        if r1 > r2:
            r1, r2 = r2, r1
        # Walk the target sheet's cells
        sheet_cells = all_sheets.get(sheet_name) if sheet_name else None
        if sheet_cells is None:
            return []
        seen: list[str] = []
        for cell in sheet_cells:
            if r1 <= cell.row <= r2 and c1 <= cell.col <= c2:
                v = cell.value
                if v is None:
                    continue
                sv = str(v).strip()
                if sv and sv not in seen:
                    seen.append(sv)
        return seen[:50]

    # Literal: comma-separated. Excel can use ; in some locales — try both.
    for sep in (",", ";"):
        if sep in s:
            parts = [p.strip().strip('"') for p in s.split(sep)]
            return [p for p in parts if p][:50]

    # Single literal value
    return [s.strip('"')] if s else []


def _col_to_num(col_str: str) -> int | None:
    """Excel column letters -> 1-indexed integer ('A' -> 1, 'AA' -> 27)."""
    if not col_str:
        return None
    n = 0
    for ch in col_str.upper():
        if not ("A" <= ch <= "Z"):
            return None
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _extract_conditional_formats(ws, sheet_name: str) -> list[ConditionalFormatRule]:
    out: list[ConditionalFormatRule] = []
    try:
        cfs = ws.conditional_formattings
    except Exception:
        return out
    for cf in cfs:
        # cell-area list lives on .cell_area or .cell_area_list depending on version
        ranges: list[str] = []
        for area_attr in ("cell_area_list", "cell_areas"):
            try:
                areas = getattr(cf, area_attr, None)
                if areas is None:
                    continue
                for area in areas:
                    ranges.append(
                        f"{column_letter(area.start_column + 1)}{area.start_row + 1}:"
                        f"{column_letter(area.end_column + 1)}{area.end_row + 1}"
                    )
                if ranges:
                    break
            except Exception:
                continue
        cell_range = ",".join(ranges)
        try:
            conditions = cf.format_conditions
        except Exception:
            conditions = []
        # Emit at least one record per CF entry even when we couldn't read the
        # conditions list. That way the LLM knows a conditional format exists
        # over this range, even if we couldn't decode its rule.
        emitted_any = False
        for cond in conditions or []:
            try:
                formula = ""
                try:
                    formula = cond.formula1 or ""
                except Exception:
                    pass
                out.append(ConditionalFormatRule(
                    sheet_name=sheet_name,
                    cell_range=cell_range,
                    rule_type=str(cond.type).rsplit(".", 1)[-1].lower() if cond.type is not None else "",
                    operator=str(cond.operator).rsplit(".", 1)[-1].lower() if cond.operator is not None else "",
                    formula=formula,
                ))
                emitted_any = True
            except Exception:
                continue
        if not emitted_any:
            out.append(ConditionalFormatRule(
                sheet_name=sheet_name,
                cell_range=cell_range,
                rule_type="unknown",
                operator="",
                formula="",
                description="conditional formatting exists but rule details could not be read",
            ))
    return out


def parse_workbook(file_path: Path) -> ParsedWorkbook:
    """Full single-pass parse of an Excel workbook via Aspose.Cells."""
    result = ParsedWorkbook()
    result.metadata.filename = file_path.name
    result.metadata.file_size_bytes = file_path.stat().st_size

    logger.info(f"Loading workbook via Aspose.Cells: {file_path.name}")
    try:
        wb = Workbook(str(file_path))
    except Exception as e:
        logger.error(f"Failed to load workbook: {e}")
        raise

    result.metadata.has_vba = _detect_vba(file_path)
    result.metadata.sheet_count = len(wb.worksheets)

    # Document properties — File > Properties (Title/Author/Company/etc.).
    # Often literally describes the template's purpose in the author's words.
    doc_props = _extract_document_properties(wb)
    result.metadata.title = doc_props["title"]
    result.metadata.author = doc_props["author"]
    result.metadata.company = doc_props["company"]
    result.metadata.subject = doc_props["subject"]
    result.metadata.keywords = doc_props["keywords"]
    result.metadata.comments = doc_props["comments"]
    result.metadata.last_modified_by = doc_props["last_modified_by"]
    result.metadata.created_date = doc_props["created_date"]
    result.metadata.modified_date = doc_props["modified_date"]

    # Named ranges (workbook-scope)
    try:
        named = wb.worksheets.get_named_ranges()
        if named is not None:
            for nr in named:
                try:
                    dests = [nr.refers_to or ""]
                except Exception:
                    dests = []
                result.named_ranges.append(NamedRange(
                    name=nr.name,
                    scope=None,
                    destinations=dests,
                ))
    except Exception as e:
        logger.debug(f"named-range extract failed: {e}")
    result.metadata.total_named_ranges = len(result.named_ranges)

    all_formula_cells: dict[str, str] = {}
    all_cell_addrs: set[str] = set()
    precedents_by_cell: dict[str, list[str]] = {}

    hidden_count = 0
    total_cells = 0
    total_formulas = 0

    for sheet_idx, ws in enumerate(wb.worksheets):
        sheet_name = ws.name
        is_hidden = not ws.is_visible
        if is_hidden:
            hidden_count += 1

        parsed_sheet = ParsedSheet(name=sheet_name, index=sheet_idx, is_hidden=is_hidden)

        # Per-sheet used range from Aspose — equivalent of Excel's Ctrl+End.
        # We respect this directly; safety caps only kick in for pathological cases.
        raw_max_row = (ws.cells.max_data_row + 1) if ws.cells.max_data_row >= 0 else 0
        raw_max_col = (ws.cells.max_data_column + 1) if ws.cells.max_data_column >= 0 else 0
        max_row = min(raw_max_row, MAX_ROWS_PER_SHEET)
        max_col = min(raw_max_col, MAX_COLS_PER_SHEET)
        parsed_sheet.used_max_row = raw_max_row
        parsed_sheet.used_max_col = raw_max_col
        if raw_max_row > MAX_ROWS_PER_SHEET or raw_max_col > MAX_COLS_PER_SHEET:
            logger.warning(
                f"Sheet '{sheet_name}' exceeded safety cap: "
                f"used range {raw_max_row}r x {raw_max_col}c, "
                f"safety-capped to {max_row}r x {max_col}c. "
                f"Bump MAX_ROWS_PER_SHEET / MAX_COLS_PER_SHEET in "
                f"workbook_parser.py to widen the cap."
            )
            parsed_sheet.was_truncated = True
        parsed_sheet.row_count = max_row
        parsed_sheet.col_count = max_col

        # Merged cells
        try:
            mc = ws.cells.merged_cells
            if mc is not None:
                for ca in mc:
                    top_left = ws.cells.get(ca.start_row, ca.start_column)
                    parsed_sheet.merged_ranges.append(MergedRange(
                        range=(
                            f"{column_letter(ca.start_column + 1)}{ca.start_row + 1}:"
                            f"{column_letter(ca.end_column + 1)}{ca.end_row + 1}"
                        ),
                        min_row=ca.start_row + 1,
                        min_col=ca.start_column + 1,
                        max_row=ca.end_row + 1,
                        max_col=ca.end_column + 1,
                        value=top_left.value if top_left is not None else None,
                    ))
        except Exception as e:
            logger.debug(f"merged-cells extract failed for '{sheet_name}': {e}")

        # Iterate only non-empty cells (much faster than scanning every (r,c))
        for cell in ws.cells:
            r = cell.row + 1
            c = cell.column + 1
            if r > max_row or c > max_col:
                continue

            cell_info = analyze_cell(cell, r, c)
            if cell_info.cell_type == CellType.EMPTY:
                # Keep empty cells the author flagged as inputs — input-style
                # fill, or explicitly unlocked (a deliberate "editable" marker,
                # meaningful even when sheet protection is off). Otherwise they'd
                # be invisible to the model. All other empties are dropped.
                st = cell_info.style
                if not (is_input_fill(st.fill_color) or st.is_locked is False):
                    continue

            qualified = f"{sheet_name}!{cell_info.address}"
            parsed_sheet.cells.append(cell_info)
            all_cell_addrs.add(qualified)
            total_cells += 1

            if cell_info.formula:
                all_formula_cells[qualified] = cell_info.formula
                total_formulas += 1
                # Pull dependencies directly from Aspose — no string parsing.
                try:
                    prec = cell.get_precedents()
                except Exception:
                    prec = None
                if prec is not None:
                    # Store the resolved RANGES (lossless), not expanded cells.
                    ranges = [_referred_area_to_range(area, sheet_name) for area in prec]
                    cell_info.precedents = ranges
                    precedents_by_cell[qualified] = ranges

        parsed_sheet.regions = detect_regions(parsed_sheet.cells, sheet_name)
        parsed_sheet.comments = _extract_comments(ws, sheet_name)
        parsed_sheet.data_validations = _extract_validations(ws, sheet_name)
        parsed_sheet.conditional_formats = _extract_conditional_formats(ws, sheet_name)
        # Off-grid content — text boxes, headers/footers, charts, pictures.
        # These often carry template guidance ('fill yellow cells only', sign
        # conventions, fiscal-year notes) that lives outside the cell grid.
        parsed_sheet.text_box_notes = _extract_text_boxes(ws, sheet_name, parsed_sheet.cells)
        parsed_sheet.page_headers_footers = _extract_headers_footers(ws, sheet_name)
        parsed_sheet.chart_captions = _extract_chart_captions(ws, sheet_name)
        parsed_sheet.picture_notes = _extract_pictures(ws, sheet_name)
        parsed_sheet.hyperlinks = _extract_hyperlinks(ws, sheet_name)

        # Sheet-level metadata — tab color, frozen panes, print area, narrow cols.
        parsed_sheet.tab_color = _extract_tab_color(ws)
        parsed_sheet.frozen_rows, parsed_sheet.frozen_cols = _extract_frozen_panes(ws)
        parsed_sheet.print_area = _extract_print_area(ws)
        parsed_sheet.narrow_columns = _extract_narrow_columns(ws, max_col)

        # Excel row outline/grouping levels — author-encoded hierarchy. Only
        # instantiated rows are yielded; we keep rows with a non-zero level.
        try:
            for row_obj in ws.cells.rows:
                gl = int(getattr(row_obj, "group_level", 0) or 0)
                if gl and (row_obj.index + 1) <= max_row:
                    parsed_sheet.row_group_levels[row_obj.index + 1] = gl
        except Exception as e:
            logger.debug(f"row-group extract failed for '{sheet_name}': {e}")

        try:
            # is_protected reflects whether sheet protection is on; the
            # per-cell ``is_locked`` flag becomes meaningful only when this
            # is True.
            parsed_sheet.is_protected = bool(
                getattr(ws.protection, "is_protected_with_password", False)
                or getattr(ws, "is_protected", False)
            )
        except Exception:
            parsed_sheet.is_protected = False

        result.sheets.append(parsed_sheet)

    result.metadata.hidden_sheet_count = hidden_count
    result.metadata.total_cells = total_cells
    result.metadata.total_formulas = total_formulas

    # Formula graph straight from precedents — no string parsing needed.
    result.formula_graph = build_formula_graph_from_precedents(
        precedents_by_cell,
        formula_addrs=set(all_formula_cells.keys()),
        populated_cells=all_cell_addrs,
        formula_strings=all_formula_cells,
    )

    # Second pass — resolve any 'list'-type data validations. Needs cross-sheet
    # cell access (a list often references cells on a Lookups sheet).
    sheet_cells_by_name = {s.name: s.cells for s in result.sheets}
    for sheet in result.sheets:
        for v in sheet.data_validations:
            if v.validation_type == "list" and v.formula1 and not v.allowed_values:
                v.allowed_values = _resolve_validation_list_values(
                    v.formula1, sheet_cells_by_name,
                )

    logger.info(
        f"Parsed {total_cells} cells, {total_formulas} formulas, "
        f"{len(result.sheets)} sheets"
    )
    return result
