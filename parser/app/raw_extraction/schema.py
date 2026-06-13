"""Pydantic models for the raw Aspose extraction (Layer 1 only).

Pruned to the extraction surface the parser actually produces. The LLM /
blueprint / contract models from the original prototype were intentionally
dropped — those layers get rebuilt cleanly (closed taxonomies, DB-backed)
rather than inherited. Keep this file extraction-only.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# --- Enums ---

class CellType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    FORMULA = "formula"
    ERROR = "error"
    EMPTY = "empty"


class CellRole(str, Enum):
    HEADER = "header"
    LABEL = "label"
    INPUT = "input"
    FORMULA = "formula"
    DATA = "data"
    EMPTY = "empty"


# --- Cell models ---

class CellStyle(BaseModel):
    bold: bool = False
    italic: bool = False
    strikeout: bool = False
    font_size: float | None = None
    font_color: str | None = None
    fill_color: str | None = None
    number_format: str | None = None
    h_alignment: str | None = None
    v_alignment: str | None = None
    has_border: bool = False
    is_locked: bool = True       # Excel default; meaningful only on protected sheets
    indent_level: int = 0        # 0 = no indent, 1/2/3+ = hierarchy depth
    rotation_angle: float = 0.0  # 0 = horizontal; 90/-90 common for period headers


class CellInfo(BaseModel):
    address: str  # e.g. "A1"
    row: int
    col: int
    value: Any = None
    cached_value: Any = None
    formula: str | None = None
    cell_type: CellType = CellType.EMPTY
    role: CellRole = CellRole.EMPTY
    style: CellStyle = Field(default_factory=CellStyle)
    # Resolved precedent RANGES ("Sheet!A1" or "Sheet!B2:B742"); populated for
    # formula cells from Aspose.Cells' dependency analysis. Stored as ranges
    # (lossless, uncapped) — concrete cell edges are recovered by intersecting
    # these with the populated cells in formula_mapper.
    precedents: list[str] = Field(default_factory=list)


# --- Rich metadata models ---

class CellComment(BaseModel):
    cell_address: str
    sheet_name: str
    author: str = ""
    text: str = ""


class DataValidationRule(BaseModel):
    sheet_name: str
    cell_range: str  # e.g. "C5:C50"
    validation_type: str = ""  # list, whole, decimal, date, textLength, custom
    formula1: str = ""  # allowed values or formula
    formula2: str = ""  # for between/notBetween
    operator: str = ""  # between, notBetween, equal, greaterThan, etc.
    allow_blank: bool = True
    prompt_title: str = ""
    prompt_message: str = ""  # input message shown to user
    error_title: str = ""
    error_message: str = ""
    allowed_values: list[str] = Field(default_factory=list)  # resolved list values for type=list (literal split or range lookup)


class ConditionalFormatRule(BaseModel):
    sheet_name: str
    cell_range: str
    rule_type: str = ""  # cellIs, expression, colorScale, dataBar, etc.
    operator: str = ""
    formula: str = ""
    description: str = ""  # human-readable interpretation


class TextBoxNote(BaseModel):
    """Free-floating text on a sheet — text box, callout, banner, etc.

    These often hold template guidance ('fill yellow cells only', sign
    conventions, definitions) that lives outside the cell grid and would
    otherwise be invisible to the analysis pipeline.
    """
    sheet_name: str
    name: str = ""           # shape name if set in the file
    text: str = ""           # visible text content
    anchor_cell: str | None = None      # top-left anchor address e.g. "P3"
    coverage_range: str | None = None   # full footprint e.g. "P3:S7"; single-cell boxes equal anchor_cell
    nearby_labels: list[str] = Field(default_factory=list)  # labels the box appears to annotate, e.g. ["A4: Revenue", "A5: COGS"]
    shape_type: str = "text_box"  # text_box, callout, banner, arrow, group, rectangle, other


class PageHeaderFooter(BaseModel):
    """Page-setup header/footer text — appears when printing, but often
    encodes title block / version / preparer info that's load-bearing."""
    sheet_name: str
    kind: str  # "header" or "footer"
    section: str  # "left", "center", "right"
    text: str


class ChartCaption(BaseModel):
    """Chart title and axis labels — semantic context not present in cells."""
    sheet_name: str
    chart_name: str = ""
    chart_title: str = ""
    category_axis_title: str = ""
    value_axis_title: str = ""
    anchor_cell: str | None = None


class PictureNote(BaseModel):
    """Picture alt-text / title — accessibility fields often used by template
    authors to embed guidance that's invisible to most readers."""
    sheet_name: str
    name: str = ""
    alternative_text: str = ""
    title: str = ""
    anchor_cell: str | None = None


# --- Structure models ---

class MergedRange(BaseModel):
    range: str  # e.g. "A1:N1"
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    value: Any = None


class NamedRange(BaseModel):
    name: str
    scope: str | None = None  # sheet name or None for workbook-level
    destinations: list[str] = Field(default_factory=list)  # cell ranges


class Hyperlink(BaseModel):
    """Cell hyperlink — internal navigation or external URL.

    Internal navigation links reveal the author's mental map of the workbook
    (e.g. "Cover sheet has links to every section"). External links point at
    source data, definitions, methodology documents.
    """
    sheet_name: str
    cell_address: str
    display_text: str = ""
    url: str = ""               # full target — http URL, file path, or #SheetName!Cell
    is_internal: bool = False   # True if target is another sheet in the same workbook
    target_sheet: str | None = None  # if internal, the destination sheet name
    target_cell: str | None = None   # if internal, the destination cell address
    tooltip: str = ""           # screen tip / hover text


class DetectedRegion(BaseModel):
    id: str
    sheet_name: str
    cell_range: str  # e.g. "A4:N45"
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    row_count: int
    col_count: int
    cell_count: int = 0
    formula_count: int = 0
    input_count: int = 0
    label_count: int = 0
    region_type: str = "data_block"  # header_block, data_table, etc.


class FormulaLink(BaseModel):
    source: str  # e.g. "Sheet1!B5" or a range "Sheet1!B2:B742"
    target: str  # e.g. "Sheet1!B15"
    formula: str  # the formula string


# --- Workbook-level ---

class WorkbookMetadata(BaseModel):
    filename: str
    file_size_bytes: int = 0
    sheet_count: int = 0
    hidden_sheet_count: int = 0
    total_cells: int = 0
    total_formulas: int = 0
    total_named_ranges: int = 0
    has_vba: bool = False
    # Excel File > Properties — often contains the author's own description
    title: str = ""
    author: str = ""
    company: str = ""
    subject: str = ""
    keywords: str = ""
    comments: str = ""        # File > Properties > Comments (the author's free-text)
    last_modified_by: str = ""
    created_date: str | None = None    # ISO format
    modified_date: str | None = None


class SheetCellData(BaseModel):
    """Cell data bundle (used by ParsedSheet.to_cell_data)."""
    cells: list[CellInfo] = Field(default_factory=list)
    merged_ranges: list[MergedRange] = Field(default_factory=list)
    row_count: int = 0
    col_count: int = 0
