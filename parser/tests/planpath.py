"""Test helper: the legacy ``bind(...)`` call shape, composed from the Fill-Plan
verify + execute pipeline (binding.py is deleted). Lets the historical binder
regression tests keep exercising the REAL path with minimal churn: blocking
verifier issues surface as 'held for review' unmatched entries, exactly as
run.py composes them.
"""

from __future__ import annotations

from app.population.execute import _col_letters, execute_plan  # noqa: F401 (re-export)
from app.population.verify import blocked_metrics, verify_plan


def bind(facts, catalogue, metric_maps, demand, *, display_unit=None,
         confidence_floor: float = 0.6, template_context=None, agg_membership=None):
    issues = verify_plan(metric_maps, catalogue, facts, demand,
                         template_context or ({}, {}, {}), agg_membership,
                         confidence_floor=confidence_floor)
    links, unmatched, _exec_issues = execute_plan(
        facts, catalogue, metric_maps, demand, display_unit=display_unit,
        template_context=template_context, blocked=blocked_metrics(issues))
    return links, unmatched
