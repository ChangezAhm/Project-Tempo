"""Grounded per-sheet understanding — the LLM's structured output (Layer 3).

Designed for Claude structured outputs: every field is REQUIRED (optionals are
nullable rather than omitted) and objects forbid extras, so the
Pydantic-derived JSON schema is strict-compatible. Recursion is avoided —
hierarchy is expressed by referencing another item's cell address / local id,
not by nesting.

Every interpretive item carries `evidence` (real cell addresses from the grid)
and `confidence` (0-1) so claims are auditable and the uncertain are flagged.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    # Tolerate (drop) unknown extra fields rather than fail validation. The model
    # occasionally attaches a field the schema doesn't have (e.g. `evidence` on a
    # model that lacks it); forbidding extras would drop the whole sheet. The
    # prompt schema (to_strict_schema) still tells the model exactly what to emit,
    # and the grounding report still validates cited cells — so reliability wins
    # without losing correctness.
    model_config = ConfigDict(extra="ignore")


class SheetRole(str, Enum):
    input = "input"
    calc = "calc"
    lookup = "lookup"
    data_dump = "data_dump"
    cover = "cover"
    instructions = "instructions"
    mixed = "mixed"


class SectionType(str, Enum):
    income_statement = "income_statement"
    balance_sheet = "balance_sheet"
    cash_flow = "cash_flow"
    covenant = "covenant"
    debt_schedule = "debt_schedule"
    valuation = "valuation"
    cap_table = "cap_table"
    kpi = "kpi"
    assumptions = "assumptions"
    input_block = "input_block"
    lookup_table = "lookup_table"
    reconciliation = "reconciliation"
    instructions = "instructions"
    cover = "cover"
    other = "other"


class MetricType(str, Enum):
    currency = "currency"
    percentage = "percentage"
    multiple = "multiple"
    ratio = "ratio"
    count = "count"
    date = "date"
    text = "text"
    other = "other"


class ValueRole(str, Enum):
    input = "input"
    formula = "formula"
    subtotal = "subtotal"
    total = "total"
    header = "header"
    other = "other"


class InterpretationSource(str, Enum):
    template_stated = "template_stated"    # the template itself defines it — cite the source cell in evidence
    model_knowledge = "model_knowledge"    # standard PE/finance meaning the template does NOT state
    inferred = "inferred"                  # reasoned from this sheet's formulas / structure / context


class RuleCategory(str, Enum):
    unit_convention = "unit_convention"
    sign_convention = "sign_convention"
    input_selection = "input_selection"
    deadline = "deadline"
    validation = "validation"
    scope = "scope"
    exclusion = "exclusion"
    calculation_note = "calculation_note"
    other = "other"


class Section(_Strict):
    id: str                       # local id, e.g. "s1" — referenced by metric rows / sub-sections
    title: str
    section_type: SectionType
    purpose: str                  # one line: what this block is for / collects
    cell_range: str               # e.g. "C30:X78"
    parent_id: str | None         # nesting via local id; null for top-level
    confidence: float = 0.5
    evidence: list[str]           # cell addresses supporting this


class MetricRow(_Strict):
    label: str
    label_as_written: str         # the label text VERBATIM, exactly as it appears in the cell
    label_cell: str               # exact address from the grid, e.g. "D41"
    section_id: str | None        # which Section.id this belongs to
    parent_label_cell: str | None # hierarchy: the parent metric row's label_cell
    canonical_metric: str | None  # e.g. "reported_ebitda","adjusted_ebitda","net_debt","leverage","revenue"
    metric_type: MetricType
    value_role: ValueRole
    unit: str | None              # "£m","%","x","#"
    sign_convention: str | None   # e.g. "costs entered negative"
    # --- interpretation layer (business logic) — populate for INPUT / non-obvious
    # rows; leave null for plain totals/subtotals/formulas. interpretation_source
    # flags provenance so model knowledge is never mistaken for template fact.
    definition: str | None = None              # what this line item means
    qualification_criteria: str | None = None  # what would / would NOT qualify to be entered here
    expected_source: str | None = None         # where the value comes from ("management accounts", "deal model"…)
    interpretation_source: InterpretationSource | None = None
    confidence: float = 0.5
    evidence: list[str]


class Period(_Strict):
    label: str                    # "CY2025","Dec-25","LTM(2yr)"
    granularity: str              # monthly|quarterly|annual|LTM|YTD|other
    cell: str                     # header cell address
    orientation: str              # "column" | "row"
    status: str                   # historical|current|future|budget|unknown
    confidence: float = 0.5
    evidence: list[str] = []      # header/source cells (the model often supplies these)


class InputField(_Strict):
    label: str
    cells: list[str]              # actual cell addresses the user fills in
    metric_row_label_cell: str | None
    needs_value: bool             # currently empty / awaiting collection
    notes: str | None
    confidence: float = 0.5
    evidence: list[str]


class AuthorRule(_Strict):
    rule_category: RuleCategory
    raw_text: str                 # VERBATIM author wording (from a text box / validation / instruction)
    summary: str                  # one-line normalised version
    source_cell: str | None       # anchor of the text box / the validated cell, if locatable
    is_strict: bool               # imperative (MUST/ONLY/DO NOT) vs softer note
    confidence: float = 0.5
    evidence: list[str] = []      # supporting cells (the model often supplies these)


class SheetUnderstanding(_Strict):
    sheet_name: str
    role: SheetRole
    label_columns: list[int]      # which columns hold row labels (may NOT be A/B/C)
    summary: str                  # 2-4 sentences: what this sheet is and does
    sections: list[Section]
    metric_rows: list[MetricRow]
    periods: list[Period]
    input_fields: list[InputField]
    author_rules: list[AuthorRule]


# --- Workbook-level understanding (Phase 3 synthesize) ---

class SheetRoleAssessment(_Strict):
    sheet: str
    role: SheetRole
    one_line: str                 # what this sheet does, in the workbook's data flow


class DataFlowEdge(_Strict):
    from_sheet: str
    to_sheet: str                 # to_sheet's formulas read from from_sheet
    what: str                     # what flows (e.g. "Reported EBITDA + perimeter adjustments")
    confidence: float = 0.5
    graph_supported: bool | None = None   # set by the deterministic verifier (leave null)


class MetricReconciliation(_Strict):
    metric: str                   # canonical name or label
    occurrences: list[str]        # ["PortCo_Input!P41", "Quarterly_Output!AL11"]
    note: str                     # how they relate (same figure / derived / restated)
    confidence: float = 0.5


class WorkbookRule(_Strict):
    category: str                 # covenant | sign_convention | unit_convention | scope | calculation_note | other
    description: str
    applies_to: list[str]         # sheet names (empty = whole workbook)
    is_strict: bool
    evidence: list[str]           # sheet!cell or sheet names
    confidence: float = 0.5


class ImpactChain(_Strict):
    name: str                     # e.g. "Reported EBITDA → Valuation"
    start: str                    # the input, as sheet!cell (or a clear description)
    flows_to: list[str]           # sheets and/or sheet!cells it ultimately affects
    significance: str             # why it matters
    confidence: float = 0.5
    graph_supported: bool | None = None   # set by the deterministic verifier (leave null)


class WorkbookUnderstanding(_Strict):
    archetype: str                # e.g. "PE valuation / IPV workbook"
    purpose: str
    audience: str
    summary: str                  # 3-6 sentences: the whole workbook at a glance
    input_surface_sheets: list[str]   # sheets the portfolio company fills in
    sheet_roles: list[SheetRoleAssessment]
    data_flow: list[DataFlowEdge]
    metric_reconciliations: list[MetricReconciliation]
    business_rules: list[WorkbookRule]
    impact_chains: list[ImpactChain]
    review_flags: list[str]       # uncertain items a human reviewer should check
