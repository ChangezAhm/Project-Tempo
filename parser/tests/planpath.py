"""Test helper: the legacy ``bind(...)`` call shape, composed from the Fill-Plan
verify + execute pipeline (binding.py is deleted). Lets the historical binder
regression tests keep exercising the REAL path with minimal churn: blocking
verifier issues surface as 'held for review' unmatched entries, exactly as
run.py composes them.
"""

from __future__ import annotations

from datetime import date

from app.population.execute import _col_letters, execute_plan  # noqa: F401 (re-export)
from app.population.periods import align_slot
from app.population.verify import blocked_metrics, verify_plan


def pick_column(period_index: int | None, period_count: int, parsed_date: date | None,
                period_cols: list[tuple[int, date | None, str]], grain: str,
                template_grain: str | None = None, point_in_time: bool = False) -> int | None:
    """Single-column legacy shape of align_slot (test-only, like ``bind``). With
    no rollup semantics only 'single' picks arise, so behaviour is identical to
    the historical function."""
    res = pick_columns(period_index, period_count, parsed_date, period_cols, grain,
                       template_grain=template_grain, point_in_time=point_in_time)
    return res[0][0] if res else None


def pick_columns(period_index: int | None, period_count: int, parsed_date: date | None,
                 period_cols: list[tuple[int, date | None, str]], grain: str,
                 template_grain: str | None = None, point_in_time: bool = False,
                 rollup: str | None = None) -> tuple[list[int], str] | None:
    """align_slot without the failure reason (legacy shape; the point_in_time
    shim maps to rollup='end')."""
    res, _why = align_slot(period_index, period_count, parsed_date, period_cols, grain,
                           template_grain=template_grain,
                           rollup=("end" if point_in_time and rollup is None else rollup))
    return res


def bind(facts, catalogue, metric_maps, demand, *, display_unit=None,
         confidence_floor: float = 0.6, template_context=None, agg_membership=None):
    issues = verify_plan(metric_maps, catalogue, facts, demand,
                         template_context or ({}, {}, {}), agg_membership,
                         confidence_floor=confidence_floor)
    links, unmatched, _exec_issues = execute_plan(
        facts, catalogue, metric_maps, demand, display_unit=display_unit,
        template_context=template_context, blocked=blocked_metrics(issues))
    return links, unmatched
