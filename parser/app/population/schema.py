"""Population schema — types for filling a template's input slots from a source.

The LLM never reads or writes numbers: its only output is a meaning mapping
(MetricMap) from a template metric to a source SERIES. Deterministic code
(catalogue → binding → apply) reads the real value from the source snapshot at the
bound address, scales/signs it, and writes it into the template cell
with full attribution.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- Resolved link + results ------------------------------------------------

class CellLink(_M):
    template_sheet: str
    template_cell: str
    source_sheet: str
    source_cell: str
    # AGGREGATION: additional source cells ("Sheet!A1") whose values are SUMMED
    # with source_cell to fill one template line (e.g. Total = NA + EMEA + APAC).
    # Empty for a normal 1:1 link. A derived fill stays fully auditable — every
    # operand is a real cited cell and the note shows the arithmetic.
    agg_source_cells: list[str] = []
    unit_scale: float = 1.0
    sign_flip: bool = False
    confidence: float = 0.5
    note: str | None = None


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
    unmatched: list[dict] = []    # template input facts with no usable source value, + reason
    skipped: list[dict] = []      # target cells the matcher rejected as non-inputs, + reason
    summary: dict = {}


# --- v4 matcher: text-only metric->series mapping (no images, no values) ----

class MetricMap(_M):
    """One template metric mapped to one source SERIES (a labelled row across the
    source's period columns). The LLM decides MEANING only; deterministic binding
    reads the values, scales, and aligns periods."""
    metric: str                   # template metric key (canonical_metric or metric_label)
    series_id: str | None = None  # source series id from the catalogue, or null if none fits
    # AGGREGATION: extra source series ids to SUM with series_id, used ONLY when the
    # template metric is the exact arithmetic total of several source lines and the
    # source has no single series that already means it. Never used to fake a split.
    also_series_ids: list[str] = []
    # How the metric is resolved:
    #   direct      — one source series means it (series_id).
    #   aggregate   — exact SUM of series_id + also_series_ids (auto-filled).
    #   reconcile   — source carries the same amount cut DIFFERENTLY; the fill is a
    #                 provisional assumption (flagged + raised for user confirmation).
    #   unavailable — source has no data for it (series_id null); note says why.
    status: str = "direct"
    assumption: str | None = None  # reconcile: plain-English statement of what was assumed
    sign_flip: bool = False       # source/template sign conventions differ (e.g. costs +ve in source)
    confidence: float = 0.5
    note: str | None = None


class MappingOut(_M):
    mappings: list[MetricMap] = []
