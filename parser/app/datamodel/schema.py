"""Layer 4 — the dimensional data model.

A template is a dataset rendered into a grid: every input cell is a FACT at
coordinates (metric, period, scenario, basis, as-of-date, entity, unit). These
models are the derived, reviewable representation that population later targets
and that the Template Contract locks. `extra="ignore"` for the same
reliability reason as the Layer-3 models.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Scenario(str, Enum):
    actual = "actual"
    budget = "budget"
    forecast = "forecast"
    unknown = "unknown"


class Basis(str, Enum):
    point_in_time = "point_in_time"   # a stock measured at the period end (balance sheet)
    flow = "flow"                     # a flow over the period (P&L / cash flow)
    ytd = "ytd"
    trailing = "trailing"             # LTM
    unknown = "unknown"


class Provenance(str, Enum):
    deterministic = "deterministic"
    llm = "llm"
    user = "user"
    default = "default"


class DataPoint(_Model):
    fact_key: str                     # content-addressed identity (survives cells moving)
    # --- cell binding ---
    sheet_name: str
    cell: str                         # e.g. "AD20"
    row: int
    col: int                          # 1-based
    metric_row_id: str | None         # L2 template_metric_rows.id, when matched
    # --- dimensions ---
    metric_label: str
    canonical_metric: str | None
    # Period is RELATIVE to the (population-time) as-of date: period_index is the
    # column's ordinal on the timeline. parsed_date/period_label are only set for
    # templates with static date headers; here they're often null because the
    # timeline is computed from the as-of date the user supplies at population.
    period_index: int | None
    period_label: str | None
    parsed_date: str | None
    period_type: str | None
    scenario: Scenario
    basis: Basis
    entity: str | None
    unit: str | None
    currency: str | None
    # --- validation metadata (from L3 interpretation) ---
    value_role: str | None
    sign_convention: str | None
    qualification_criteria: str | None
    definition: str | None
    expected_source: str | None
    needs_value: bool
    # data | config | exclude — a correction can re-categorise a slot (e.g. a
    # selector/control input that isn't a reporting data point).
    category: str = "data"
    # --- provenance / audit ---
    scenario_source: Provenance
    basis_source: Provenance
    confidence: float = 0.5
    applied_correction_ids: list[str] = []


class DetectedDimensions(_Model):
    archetype: str | None
    timeline_relative: bool          # True when periods resolve from the as-of date (no static dates)
    base_currency: str | None
    entities: list[str]
    scenarios: list[str]
    period_grains: list[str]
    sheet_count: int
    fact_count: int
    review_flags: list[str]


class DataModelResult(_Model):
    dimensions: DetectedDimensions
    facts: list[DataPoint]
