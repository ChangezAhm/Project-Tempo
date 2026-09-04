"""RunState — every value the population stages share, made explicit — plus the
uniform best-effort guard (docs/Refactor-Population-Pipeline.md).

The monolith threaded ~40 locals through 1,100 lines; a bug class this refactor
kills is "stage B read a variable stage A only sets on one branch". Fields here
carry defaults, so a stage can always read its inputs and a skipped stage leaves
a well-defined value, never a NameError.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from time import gmtime, strftime

from app.population.cost import SpendCapExceeded

logger = logging.getLogger(__name__)


@dataclass
class RunState:
    # ---- request (populate_from_bytes / populate_from_snapshot) -------------
    target_template_id: str
    source_snapshot: dict
    source_periods: dict
    source_label: str
    as_of_date: str | None
    content_hash: str | None = None
    source_path: Path | None = None
    display_unit: str | None = None
    reset: str = "values"
    add_lines: str = "apply"
    dry_run: bool = False
    deep_rescue: bool = True
    link_sources: bool = True

    # ---- run identity --------------------------------------------------------
    # Per-run stamp for storage artifacts: two runs whose sources share a
    # filename must never overwrite each other (a user's add-in run once
    # silently replaced the previous run's audit).
    run_stamp: str = field(default_factory=lambda: strftime("%Y%m%d-%H%M%S", gmtime()))
    as_of: date | None = None

    # ---- demand (stage_demand) ----------------------------------------------
    demand: dict = field(default_factory=dict)
    target_inputs: list = field(default_factory=list)

    # ---- template side (stage_load_template) --------------------------------
    t_vid: str | None = None
    tgt_tmp: Path | None = None
    t_snap: dict | None = None
    template_context: tuple = ({}, {}, {})
    template_check_cells: list = field(default_factory=list)
    biz_context: str = ""

    # ---- grids (stage_grids) -------------------------------------------------
    grids_block: str | None = None
    grid_images: list = field(default_factory=list)
    grid_notes: list = field(default_factory=list)

    # ---- plan cache / claims / catalogue (understand.*) ----------------------
    plan_key: str | None = None
    plan_cached: bool | None = None
    plan_stage: str | None = None
    catalogue: dict | None = None
    onepass_maps: list | None = None
    translate_notes: list = field(default_factory=list)
    geometry_flags: list = field(default_factory=list)
    source_recon: dict = field(default_factory=dict)
    catalogue_source: str = "digest"

    # ---- mapping / plan (plan.*) ---------------------------------------------
    metric_maps: list = field(default_factory=list)
    agg_membership: dict | None = None
    decisions: dict = field(default_factory=dict)
    src_fp: str = ""
    scen_equiv: dict = field(default_factory=dict)
    plan_issues: list = field(default_factory=list)
    blocked: dict = field(default_factory=dict)
    links: list | None = None
    bind_unmatched: list = field(default_factory=list)

    # ---- report (report.*) ---------------------------------------------------
    routing: dict = field(default_factory=dict)
    review: list = field(default_factory=list)
    notes: list | None = None
    coverage_notes: list = field(default_factory=list)
    coverage_summary: list = field(default_factory=list)
    reconciled_metrics: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    unused_source_series: list = field(default_factory=list)
    unmapped_metrics: list = field(default_factory=list)
    unmatched_reasons: list = field(default_factory=list)
    label_by_key: dict = field(default_factory=dict)
    violations: list = field(default_factory=list)
    result: object = None   # apply.ApplyResult — set by stage_report

    # ---- additions / render / audit (deliver.*) ------------------------------
    proposals: list = field(default_factory=list)
    add_notes: list = field(default_factory=list)
    additions_applied: list = field(default_factory=list)
    additions_skipped: list = field(default_factory=list)
    pending_overwrites: list = field(default_factory=list)
    writable_proposals: list = field(default_factory=list)
    sval: dict = field(default_factory=dict)
    clear_stats: dict = field(default_factory=dict)
    check_results: list = field(default_factory=list)
    template_checks: dict = field(default_factory=dict)
    linked_stats: dict = field(default_factory=dict)
    filled_url: str | None = None
    linked_url: str | None = None
    audit_url: str | None = None

    # ---- review questions ----------------------------------------------------
    # ALL review questions for this run collect here and are filed ONCE at the
    # end through the budgeted gate (review.items.file_questions): few, binary,
    # current — a new run supersedes the previous run's unanswered questions.
    pending_q: list = field(default_factory=list)

    def ask(self, items: list[dict], priority: int = 5) -> None:
        for it in items:
            it["_priority"] = priority
        self.pending_q.extend(items)


@contextmanager
def best_effort(state: RunState, what: str, *, cap_stops_run: bool = True,
                exc_info: bool = False):
    """The pipeline's uniform failure policy: a guarded stage logs, records a
    visible `routing["stage_warnings"]` entry, and never sinks the fill. A spend
    cap breach still stops the run wherever it did pre-refactor
    (``cap_stops_run=False`` marks the blocks that deliberately swallow it —
    no LLM call inside, so a breach can't originate there)."""
    try:
        yield
    except SpendCapExceeded:
        if cap_stops_run:
            raise
        logger.warning("%s hit the spend cap — skipped", what)
        state.routing.setdefault("stage_warnings", []).append(f"{what}: spend cap")
    except Exception as e:  # noqa: BLE001 — the whole point of this guard
        if exc_info:
            logger.exception("%s failed", what)
        else:
            logger.warning("%s failed: %s", what, e)
        state.routing.setdefault("stage_warnings", []).append(f"{what}: {str(e)[:160]}")
