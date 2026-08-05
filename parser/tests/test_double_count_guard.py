"""Graph-scoped double-count guard: the template's own formulas decide whether a
source series reused across two metrics would inflate a shared total (block) or is
a KPI mirrored across independent places (allow). Covers aggregation.py's formula
analysis and bind()'s guard in both graph and fallback modes."""

from datetime import date

from app.population.aggregation import (
    _is_aggregation,
    metric_totals,
    total_leaf_rows,
)
from planpath import bind
from app.population.catalogue import build_catalogue
from app.population.schema import MetricMap


# --- aggregation-formula classification --------------------------------------
def test_is_aggregation_matrix():
    assert _is_aggregation("=SUM(C15:C17)")
    assert _is_aggregation("=SUBTOTAL(9,C1:C9)")
    assert _is_aggregation("=C15+C16+C17")          # additive bridge
    assert _is_aggregation("=C8-C9")                # subtractive combine (Net Debt)
    assert not _is_aggregation("=C18")              # a pure reference
    assert not _is_aggregation("=B5*C5")            # a product, not a sum
    assert not _is_aggregation("")
    assert not _is_aggregation(None)


# --- leaf resolution over the formula graph ----------------------------------
def _bridge_snapshot():
    """Flash: an Adjusted-EBITDA bridge (r15 reported + r16/r17 adjustments summed
    into r18), a NESTED grand total r19=r18, and a standalone ARR at r25 that feeds
    nothing. Dashboard: ARR again at r5, feeding nothing."""
    return {"sheets": [
        {"name": "Flash", "cells": [
            {"row": 15, "col": 3, "address": "C15", "value": 100.0},                       # Reported EBITDA (input)
            {"row": 16, "col": 3, "address": "C16", "value": 5.0},                         # Adjustment A (input)
            {"row": 17, "col": 3, "address": "C17", "value": 3.0},                         # Adjustment B (input)
            {"row": 18, "col": 3, "address": "C18", "formula": "=SUM(C15:C17)",
             "precedents": ["Flash!C15:C17"]},                                             # Adjusted EBITDA (total)
            {"row": 19, "col": 3, "address": "C19", "formula": "=C18+C15",
             "precedents": ["Flash!C18", "Flash!C15"]},                                    # grand total (NESTED: sums a total)
            {"row": 25, "col": 3, "address": "C25", "value": 12.0},                        # ARR (feeds nothing)
        ]},
        {"name": "Dashboard", "cells": [
            {"row": 5, "col": 3, "address": "C5", "value": 12.0},                          # ARR mirror
        ]},
    ]}


def test_total_leaf_rows_resolves_direct_and_nested():
    leaves = total_leaf_rows(_bridge_snapshot())
    # the subtotal sums its three input rows (not itself)
    assert leaves[("Flash", 18)] == {("Flash", 15), ("Flash", 16), ("Flash", 17)}
    # the nested grand total resolves THROUGH the subtotal to the same input rows
    assert leaves[("Flash", 19)] == {("Flash", 15), ("Flash", 16), ("Flash", 17)}
    # ARR rows feed no aggregation at all
    assert ("Flash", 25) not in {r for rows in leaves.values() for r in rows}


def _facts_bridge():
    def f(sheet, row, metric):
        return {"sheet_name": sheet, "row": row, "canonical_metric": metric,
                "metric_label": metric}
    return [
        f("Flash", 16, "Adjustment A"), f("Flash", 17, "Adjustment B"),
        f("Flash", 25, "ARR"), f("Dashboard", 5, "ARR (dashboard)"),
    ]


def test_metric_totals_membership_shared_vs_standalone():
    m = metric_totals(_facts_bridge(), _bridge_snapshot())
    # both adjustments feed the SAME totals (subtotal + grand total) -> conflict
    assert m["Adjustment A"] == m["Adjustment B"]
    assert m["Adjustment A"] & m["Adjustment B"]
    # ARR on either sheet feeds nothing -> empty membership -> safe to repeat
    assert m["ARR"] == frozenset() and m["ARR (dashboard)"] == frozenset()


def test_metric_totals_none_when_no_aggregations():
    flat = {"sheets": [{"name": "S", "cells": [
        {"row": 1, "col": 1, "address": "A1", "value": 5.0}]}]}
    assert metric_totals([{"sheet_name": "S", "row": 1, "canonical_metric": "x"}], flat) is None
    assert metric_totals([], None) is None


# --- the guard end to end through bind() -------------------------------------
def _cat_two_series():
    # one source ARR series and one source adjustment series
    snap = {"sheets": [{"name": "Src", "cells": [
        {"row": 5, "col": 1, "value": "ARR", "address": "A5"},
        {"row": 5, "col": 3, "value": 12_000_000, "address": "C5"},
        {"row": 6, "col": 1, "value": "Restructuring add-back", "address": "A6"},
        {"row": 6, "col": 3, "value": 5_000_000, "address": "C6"},
    ]}]}
    return build_catalogue(snap, {"Src": [{"col": 3, "parsed_date": "2023-12", "period_type": "month"}]})


def _tfact(metric, cell, sheet, row):
    return {"sheet_name": sheet, "cell": cell, "row": row, "canonical_metric": metric,
            "metric_label": metric, "unit": None, "currency": None,
            "period_index": 0, "scenario": "actual", "parsed_date": None}


def _demand1():
    return {"period_count": 1, "period_grain": "monthly", "as_of_date": None,
            "period_count_by_sheet": {"Flash": 1, "Dashboard": 1}, "metrics": []}


def test_graph_allows_kpi_mirror_across_sheets():
    # SAME source ARR series mapped to ARR on Flash AND on the Dashboard. They
    # share no total -> BOTH must fill (this is the bug we're fixing).
    cat = _cat_two_series()
    maps = [
        MetricMap(metric="ARR", series_id="Src!r5", confidence=0.9, source_unit="EUR", target_unit="EUR"),
        MetricMap(metric="ARR (dashboard)", series_id="Src!r5", confidence=0.9, source_unit="EUR", target_unit="EUR"),
    ]
    facts = [_tfact("ARR", "C25", "Flash", 25),
             _tfact("ARR (dashboard)", "C5", "Dashboard", 5)]
    membership = metric_totals(_facts_bridge(), _bridge_snapshot())
    links, unmatched = bind(facts, cat, maps, _demand1(), agg_membership=membership)
    filled = {lk.template_cell for lk in links}
    assert filled == {"C25", "C5"}                      # both KPI copies filled
    assert not any("double-count" in u["reason"] for u in unmatched)


def test_graph_blocks_reuse_within_a_shared_total():
    # SAME source series mapped to two adjustment rows that both feed Adjusted
    # EBITDA -> writing it twice inflates the total -> the weaker one is blocked.
    cat = _cat_two_series()
    maps = [
        MetricMap(metric="Adjustment A", series_id="Src!r6", confidence=0.95, source_unit="EUR", target_unit="EUR"),
        MetricMap(metric="Adjustment B", series_id="Src!r6", status="reconcile",
                  assumption="same add-back", confidence=0.9, source_unit="EUR", target_unit="EUR"),
    ]
    facts = [_tfact("Adjustment A", "C16", "Flash", 16),
             _tfact("Adjustment B", "C17", "Flash", 17)]
    membership = metric_totals(_facts_bridge(), _bridge_snapshot())
    links, unmatched = bind(facts, cat, maps, _demand1(), agg_membership=membership)
    assert len(links) == 1 and links[0].template_cell == "C16"      # direct wins
    assert any("double-count" in u["reason"] and "same template total" in u["reason"]
               for u in unmatched)


def test_fallback_without_graph_keeps_global_block():
    # agg_membership=None (no formula graph) -> conservative: any cross-metric
    # reuse is blocked, so the inflation bug can never silently return.
    cat = _cat_two_series()
    maps = [
        MetricMap(metric="ARR", series_id="Src!r5", confidence=0.9, source_unit="EUR", target_unit="EUR"),
        MetricMap(metric="ARR (dashboard)", series_id="Src!r5", confidence=0.9, source_unit="EUR", target_unit="EUR"),
    ]
    facts = [_tfact("ARR", "C25", "Flash", 25),
             _tfact("ARR (dashboard)", "C5", "Dashboard", 5)]
    links, unmatched = bind(facts, cat, maps, _demand1(), agg_membership=None)
    assert len(links) == 1                              # global block, as before
    assert any("double-count" in u["reason"] for u in unmatched)
