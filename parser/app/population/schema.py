"""Population schema — types for filling a template's input slots from a source.

The LLM never reads or writes numbers: its only output is a meaning mapping
(MetricMap) from a template metric to a source SERIES. Deterministic code
(catalogue → binding → apply) reads the real value from the source snapshot at the
bound address, scales/signs/FX-converts it, and writes it into the template cell
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
    reads the values, scales, aligns periods, and applies FX."""
    metric: str                   # template metric key (canonical_metric or metric_label)
    series_id: str | None = None  # source series id from the catalogue, or null if none fits
    sign_flip: bool = False       # source/template sign conventions differ (e.g. costs +ve in source)
    confidence: float = 0.5
    note: str | None = None


class MappingOut(_M):
    mappings: list[MetricMap] = []
