"""Population (Build A) — fill a template's input slots from a source workbook.

The LLM produces a MAPPING (where each template metric lives in the source +
how source columns align to template periods + transforms). It never reads the
numbers. Deterministic code then reads the real values from the source snapshot
and writes them into the template's bound cells, with full attribution.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PeriodAlign(_M):
    """One source column supplies one template (relative) period."""
    source_sheet: str
    source_col: int               # 1-based
    period_index: int             # the template's relative period this column maps to
    source_period_label: str | None = None


class MetricMatch(_M):
    """A template metric is found at this source sheet+row, with transforms."""
    template_metric: str          # the canonical_metric or metric_label being matched
    scenario: str | None = None   # template scenario this supplies (None = any/single-scenario source)
    source_sheet: str
    source_row: int
    source_label: str | None = None    # verbatim source label, for audit
    unit_scale: float = 1.0       # multiply source value (e.g. thousands→millions = 0.001)
    sign_flip: bool = False
    confidence: float = 0.5
    notes: str | None = None


class PopulationMapping(_M):
    metric_matches: list[MetricMatch] = []
    period_aligns: list[PeriodAlign] = []
    unmatched_metrics: list[str] = []   # template metrics the LLM could not find in the source
    notes: list[str] = []


class FilledCell(_M):
    template_sheet: str
    template_cell: str
    value: float | str            # transformed value to write
    raw_source_value: float | str
    source_sheet: str
    source_cell: str
    metric: str
    period_index: int | None
    scenario: str | None
    confidence: float


class PopulationResult(_M):
    filled: list[FilledCell] = []
    unmatched: list[dict] = []    # template facts with no source value, + reason
    summary: dict = {}
