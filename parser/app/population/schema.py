"""Population schema — types for filling a template's input slots from a source.

The LLM never reads or writes numbers: its only output is a meaning mapping
(MetricMap) from a template metric to a source SERIES. Deterministic code
(catalogue → verify/execute → apply) reads the real value from the source snapshot at the
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
    # AGGREGATION: additional source cells ("Sheet!A1") combined with source_cell
    # to fill one template line — summed for a row aggregate (Total = NA + EMEA +
    # APAC) or a monthly->quarterly rollup of a flow; averaged (agg_op='avg') for
    # a rate rolled up across months. Empty for a normal 1:1 link. A derived fill
    # stays fully auditable — every operand is a real cited cell and the note
    # shows the arithmetic.
    agg_source_cells: list[str] = []
    agg_op: str = "sum"           # 'sum' | 'avg' — how agg_source_cells combine
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
    source's period columns). The LLM decides MEANING only; deterministic
    verify/execute reads the values, scales, and aligns periods."""
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
    # ROLLUP semantics: how a coarser template period is built from finer source
    # periods (months -> quarter/year) when grains differ. 'sum' = period flow,
    # 'end' = point-in-time stock (period-end value), 'avg' = rate/ratio. None =
    # unknown; verify raises GRAIN_UNBRIDGEABLE when grains differ.
    rollup: str | None = None
    # --- FILL-PLAN fields (docs/Fill-Plan-Architecture.md §2.2): the COMPLETE
    # semantic decision, series-level. The executor computes from these; it never
    # re-decides them. All optional so pre-plan mappings still parse.
    scenario: str | None = None    # actual|budget|forecast — the demanded scenario this
                                   # entry serves; None = every demanded scenario
    source_unit: str | None = None  # the unit AS READ from the source ("USD'000", "%", "FTE")
    target_unit: str | None = None  # the unit AS READ from the template ("EUR m", "%")
    sign_basis: str | None = None   # one line: why sign_flip is what it is
    period_map: str = "calendar"    # calendar|positional — positional ONLY when a side is
                                    # dateless in the facts; always flagged in the audit
    sign_flip: bool = False       # source/template sign conventions differ (e.g. costs +ve in source)
    confidence: float = 0.5
    note: str | None = None


class MappingOut(_M):
    mappings: list[MetricMap] = []


def metric_key(fact: dict) -> str | None:
    """The demand key a template fact maps under: the WRITTEN LABEL first, the
    canonical metric as fallback — ONE definition for demand, verify and execute.

    Label-first because the label is the template's own row identity, while
    canonical_metric is an LLM normalisation that COLLIDES across distinct
    lines: 'Gross revenue' and 'Net revenue' both canonicalised to 'Revenue',
    collapsed into one demand entry, and both rows filled from the gross
    series. Two rows sharing the same written label are genuinely the same
    quantity displayed twice (e.g. 'Reported EBITDA' opening the CF bridge)
    and correctly share one mapping."""
    return fact.get("metric_label") or fact.get("canonical_metric")


class PlanIssue(_M):
    """One typed verifier/executor finding about a plan entry. The resolution
    ladder (repair -> flagged default -> batched question -> hard block) is
    driven by ``severity``; nothing dies as a bare reason string."""
    metric: str
    code: str                     # SERIES_NOT_FOUND | PLAN_INCOMPLETE | COMPONENT_MISMATCH |
                                  # SCENARIO_NO_SOURCE | GRAIN_UNBRIDGEABLE | BUCKET_INCOMPLETE |
                                  # PERIOD_END_MISSING | SCALE_CONFLICT | UNIT_KIND_MISMATCH |
                                  # SIGN_CONFLICT | DOUBLE_COUNT | LOW_CONFIDENCE | SOURCE_GAP
    detail: str
    severity: str = "question"    # repair | default | question | block
    scenario: str | None = None    # the demanded scenario at issue (SCENARIO_NO_SOURCE)
    suggested_resolution: str | None = None
    resolution: str | None = None  # set when tier-2 auto-resolved (visible, never silent)
    cells: list[str] = []          # affected template cells ("Sheet!A1"), for batching
