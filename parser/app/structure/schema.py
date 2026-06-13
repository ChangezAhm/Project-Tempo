"""Lean Layer-2 output models (deterministic structure only).

No canonical_metric / metric_type / value_role — those are LLM-assigned in
Layer 3. These models carry structural facts the detectors produce.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PeriodStatus(str, Enum):
    HISTORICAL = "historical"
    CURRENT = "current"
    FUTURE = "future"
    BUDGET = "budget"
    YTD = "ytd"
    LTM = "ltm"
    UNKNOWN = "unknown"


class MetricRow(BaseModel):
    sheet_name: str
    row: int
    label_text: str
    label_cell: str          # e.g. "B14"
    label_col: int
    indent_level: int = 0
    parent_row: int | None = None   # local key (parent's row number); resolved to FK at persist
    data_cols: list[int] = Field(default_factory=list)
    data_range: str | None = None   # e.g. "C14:N14"
    is_formula: bool = False
    is_bold: bool = False
    is_strikethrough: bool = False
    unit: str | None = None
    number_format: str | None = None
    sample_value: Any = None
    named_range: str | None = None


class DetectedPeriod(BaseModel):
    sheet_name: str = ""
    col: int
    row: int | None = None
    label: str
    parsed_date: str | None = None
    period_type: str = ""
    status: PeriodStatus = PeriodStatus.UNKNOWN


class InputField(BaseModel):
    sheet_name: str
    row: int
    label_text: str
    label_cell: str
    input_columns: list[int] = Field(default_factory=list)
    formula_columns: list[int] = Field(default_factory=list)
    current_period_col: int | None = None
    current_period_label: str = ""
    is_unlocked: bool = False
    needs_collection: bool = False
    has_historical_data: bool = False
    unit: str | None = None
    number_format: str | None = None
    sample_value: Any = None
    named_range: str | None = None
    indent_level: int = 0
    dependent_formulas: list[str] = Field(default_factory=list)
    downstream_cells: list[str] = Field(default_factory=list)
    input_evidence: list[str] = Field(default_factory=list)


class Region(BaseModel):
    sheet_name: str
    cell_range: str
    min_row: int
    min_col: int
    max_row: int
    max_col: int
    region_type: str = "data_block"
    cell_count: int = 0
    formula_count: int = 0
    input_count: int = 0
    label_count: int = 0


class SectionTitleSignal(BaseModel):
    sheet_name: str
    row: int
    text: str
    signal_type: str = "title_candidate"


class StructureResult(BaseModel):
    metric_rows: list[MetricRow] = Field(default_factory=list)
    periods: list[DetectedPeriod] = Field(default_factory=list)
    input_fields: list[InputField] = Field(default_factory=list)
    regions: list[Region] = Field(default_factory=list)
    section_signals: list[SectionTitleSignal] = Field(default_factory=list)
