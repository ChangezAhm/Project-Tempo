"""Cross-sheet coverage completion: when the mapped ('winning') source series
doesn't cover a demanded period but another catalogue series of the SAME metric
label does (history on a second sheet), execute fills the gap from the sibling —
under the same guards, flagged — instead of leaving the cell silently blank.
This is the history/budget fix (pop-correctness P3). Only previously-blank cells
are ever touched, so a correct primary fill can never be disturbed."""

from app.population.catalogue import build_catalogue
from app.population.execute import execute_plan
from app.population.schema import MetricMap


def _snapshot(hist_label="Revenue"):
    # PRIMARY sheet: Revenue covers 2024 only. HIST sheet: same label, covers
    # 2023 AND 2024. Both in EUR millions so scale resolves identically.
    primary = [
        {"row": 1, "col": 1, "value": "EUR millions", "address": "A1"},
        {"row": 5, "col": 1, "value": "Revenue", "address": "A5"},
        {"row": 5, "col": 3, "value": 12_000_000, "address": "C5"},   # 2024-01
        {"row": 5, "col": 4, "value": 13_000_000, "address": "D5"},   # 2024-02
    ]
    hist = [
        {"row": 1, "col": 1, "value": "EUR millions", "address": "A1"},
        {"row": 5, "col": 1, "value": hist_label, "address": "A5"},
        {"row": 5, "col": 3, "value": 10_000_000, "address": "C5"},   # 2023-01
        {"row": 5, "col": 4, "value": 11_000_000, "address": "D5"},   # 2023-02
        {"row": 5, "col": 5, "value": 12_000_000, "address": "E5"},   # 2024-01
        {"row": 5, "col": 6, "value": 13_000_000, "address": "F5"},   # 2024-02
    ]
    return {"sheets": [{"name": "Primary", "cells": primary},
                       {"name": "Hist", "cells": hist}]}


def _periods():
    return {
        "Primary": [{"col": 3, "parsed_date": "2024-01", "period_type": "month"},
                    {"col": 4, "parsed_date": "2024-02", "period_type": "month"}],
        "Hist": [{"col": 3, "parsed_date": "2023-01", "period_type": "month"},
                 {"col": 4, "parsed_date": "2023-02", "period_type": "month"},
                 {"col": 5, "parsed_date": "2024-01", "period_type": "month"},
                 {"col": 6, "parsed_date": "2024-02", "period_type": "month"}],
    }


def _demand():
    return {"period_count": 2, "period_grain": "monthly", "as_of_date": None,
            "metrics": [{"metric": "revenue", "label": "Revenue", "unit": "EUR millions"}]}


def _fact(cell, parsed_date, pidx):
    return {"sheet_name": "Template", "cell": cell, "canonical_metric": "revenue",
            "metric_label": "revenue", "unit": "EUR millions", "currency": "EUR",
            "row": int(cell[1:]), "col": 2, "period_index": pidx, "scenario": "actual",
            "parsed_date": parsed_date, "period_type": "monthly"}


def test_history_gap_filled_from_sibling_sheet():
    cat = build_catalogue(_snapshot(), _periods())
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]
    facts = [_fact("B9", "2023-01", 0), _fact("B10", "2024-01", 1)]   # 2023 has no Primary column
    links, unmatched, issues = execute_plan(facts, cat, maps, _demand())

    by_cell = {lk.template_cell: lk for lk in links}
    assert set(by_cell) == {"B9", "B10"}                       # BOTH filled
    assert by_cell["B10"].source_sheet == "Primary"            # 2024 from the winner
    assert by_cell["B9"].source_sheet == "Hist"                # 2023 from the history sibling
    assert "coverage" in by_cell["B9"].note.lower()            # flagged
    assert not unmatched
    assert any(i.code == "COVERAGE_CROSS_SHEET" for i in issues)   # loud review flag


def test_history_filled_via_coverage_ids_despite_different_label():
    # THE GENERAL CASE: the history sheet labels the metric differently
    # ("Total revenue (allocated)" vs the winner's "Revenue"), so exact-label
    # matching would miss it. The mapper's coverage_series_ids — its meaning
    # judgment that these series are the same metric — bridges it.
    cat = build_catalogue(_snapshot(hist_label="Total revenue (allocated)"), _periods())
    maps = [MetricMap(metric="revenue", series_id="Primary!r5",
                      coverage_series_ids=["Hist!r5"], confidence=0.9)]
    facts = [_fact("B9", "2023-01", 0), _fact("B10", "2024-01", 1)]
    links, unmatched, issues = execute_plan(facts, cat, maps, _demand())
    by_cell = {lk.template_cell: lk for lk in links}
    assert set(by_cell) == {"B9", "B10"}
    assert by_cell["B9"].source_sheet == "Hist"            # filled despite different label
    assert "coverage" in by_cell["B9"].note.lower()
    assert not unmatched
    assert any(i.code == "COVERAGE_CROSS_SHEET" for i in issues)


def test_different_label_without_coverage_ids_stays_blank():
    # Same different-label history, but the mapper did NOT list it: no same-label
    # match and no coverage id → honestly left blank (never guessed by fuzzy label).
    cat = build_catalogue(_snapshot(hist_label="Total revenue (allocated)"), _periods())
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]
    facts = [_fact("B9", "2023-01", 0), _fact("B10", "2024-01", 1)]
    links, unmatched, _ = execute_plan(facts, cat, maps, _demand())
    by_cell = {lk.template_cell: lk for lk in links}
    assert "B10" in by_cell and "B9" not in by_cell
    assert any(u["template_cell"] == "B9" for u in unmatched)


def test_no_sibling_means_the_gap_stays_blank():
    # HIST sheet's row is labelled differently → not the same metric → no fill.
    cat = build_catalogue(_snapshot(hist_label="Something else"), _periods())
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]
    facts = [_fact("B9", "2023-01", 0), _fact("B10", "2024-01", 1)]
    links, unmatched, _ = execute_plan(facts, cat, maps, _demand())

    by_cell = {lk.template_cell: lk for lk in links}
    assert "B10" in by_cell and "B9" not in by_cell            # 2024 filled, 2023 left blank
    assert any(u["template_cell"] == "B9" for u in unmatched)  # and honestly reported


def test_coverage_completion_never_overwrites_a_primary_fill():
    # When the winner DOES cover the period, the sibling is never consulted.
    cat = build_catalogue(_snapshot(), _periods())
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]
    facts = [_fact("B10", "2024-01", 1)]
    links, _u, _i = execute_plan(facts, cat, maps, _demand())
    assert len(links) == 1 and links[0].source_sheet == "Primary"
