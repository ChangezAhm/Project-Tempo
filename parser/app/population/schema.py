"""Population (Build A, v2) — fill a template's input slots from a source workbook.

The matcher is BILATERAL and CELL-LEVEL: the LLM sees the template sheet (image +
grid + the list of input cells to fill) AND the routed source sheet(s) (image +
grid), and emits DIRECT links — template_cell → source_sheet!source_cell — with
transforms. It never reads or writes numbers. Deterministic code then reads the
real value from the source snapshot at the cited address and writes it into the
template's cell, with full attribution.

This replaces the previous design (metric-name list → per-sheet blind match →
triple exact-join on metric×scenario×period), which discarded the template's
visual context and lost cells whenever any one join key was imperfect.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- LLM outputs (forced via prompt + Pydantic validation) -----------------

class RouteOut(_M):
    """Which source sheet(s) feed one template sheet (stage 1)."""
    template_sheet: str
    source_sheets: list[str] = []


class RoutingOut(_M):
    routes: list[RouteOut] = []


class LinkOut(_M):
    """One template input cell ← one source cell, with transforms (stage 2).

    The template_sheet is fixed by the call (the batch is one template sheet), so
    the model emits only the template CELL; the sheet is attached in code.
    """
    template_cell: str            # exact A1 in the template sheet being matched
    source_sheet: str             # source sheet name (from the provided list)
    source_cell: str              # exact A1 in that source sheet
    unit_scale: float = 1.0       # multiply source→template units (thousands→millions = 0.001)
    sign_flip: bool = False       # source/template sign conventions differ
    confidence: float = 0.5
    note: str | None = None


class SkipOut(_M):
    """A listed target cell the model judges is NOT a real input (header/total/etc.)."""
    template_cell: str
    reason: str


class SheetMatchOut(_M):
    links: list[LinkOut] = []
    skipped: list[SkipOut] = []
    notes: list[str] = []


# --- Resolved link (sheet attached) + results ------------------------------

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
