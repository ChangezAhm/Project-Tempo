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


def _snapshot_unrelated():
    # History sheet with a different label AND values that DON'T match the winner
    # in the overlap — a genuinely different line; must never be connected.
    primary = [
        {"row": 1, "col": 1, "value": "EUR millions", "address": "A1"},
        {"row": 5, "col": 1, "value": "Revenue", "address": "A5"},
        {"row": 5, "col": 3, "value": 12_000_000, "address": "C5"},   # 2024-01
        {"row": 5, "col": 4, "value": 13_000_000, "address": "D5"},   # 2024-02
    ]
    hist = [
        {"row": 1, "col": 1, "value": "EUR millions", "address": "A1"},
        {"row": 5, "col": 1, "value": "Something else", "address": "A5"},
        {"row": 5, "col": 3, "value": 500.0, "address": "C5"},
        {"row": 5, "col": 4, "value": 510.0, "address": "D5"},
        {"row": 5, "col": 5, "value": 777.0, "address": "E5"},        # 2024-01 != 12,000,000
        {"row": 5, "col": 6, "value": 888.0, "address": "F5"},
    ]
    return {"sheets": [{"name": "Primary", "cells": primary}, {"name": "Hist", "cells": hist}]}


def test_unrelated_series_is_never_connected():
    # Different label AND non-matching values AND no coverage id → honestly blank.
    # Proves value-continuity does not false-positive on a genuinely different line.
    cat = build_catalogue(_snapshot_unrelated(), _periods())
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]
    facts = [_fact("B9", "2023-01", 0), _fact("B10", "2024-01", 1)]
    links, unmatched, _ = execute_plan(facts, cat, maps, _demand())
    by_cell = {lk.template_cell: lk for lk in links}
    assert "B10" in by_cell and "B9" not in by_cell            # 2024 filled, 2023 left blank
    assert any(u["template_cell"] == "B9" for u in unmatched)  # and honestly reported


def test_coverage_matched_by_template_metric_name_not_winner_label():
    # THE REAL-WORLD CASE (the Source Anchors 'Revenue' miss): the winner is a
    # differently-named line ('Total sales'), the history sheet labels the same
    # line 'Revenue' — which is the TEMPLATE's own name for it. Coverage matches
    # by the template metric name, so 2023 fills with NO coverage_series_ids and
    # despite the winner's label differing.
    primary = [
        {"row": 1, "col": 1, "value": "EUR millions", "address": "A1"},
        {"row": 5, "col": 1, "value": "Total sales", "address": "A5"},   # winner, different name
        {"row": 5, "col": 3, "value": 12_000_000, "address": "C5"},
        {"row": 5, "col": 4, "value": 13_000_000, "address": "D5"},
    ]
    hist = [
        {"row": 1, "col": 1, "value": "EUR millions", "address": "A1"},
        {"row": 5, "col": 1, "value": "Revenue", "address": "A5"},       # == template metric name
        {"row": 5, "col": 3, "value": 10_000_000, "address": "C5"},
        {"row": 5, "col": 4, "value": 11_000_000, "address": "D5"},
        {"row": 5, "col": 5, "value": 12_000_000, "address": "E5"},
        {"row": 5, "col": 6, "value": 13_000_000, "address": "F5"},
    ]
    snap = {"sheets": [{"name": "Primary", "cells": primary}, {"name": "Hist", "cells": hist}]}
    periods = {
        "Primary": [{"col": 3, "parsed_date": "2024-01", "period_type": "month"},
                    {"col": 4, "parsed_date": "2024-02", "period_type": "month"}],
        "Hist": [{"col": 3, "parsed_date": "2023-01", "period_type": "month"},
                 {"col": 4, "parsed_date": "2023-02", "period_type": "month"},
                 {"col": 5, "parsed_date": "2024-01", "period_type": "month"},
                 {"col": 6, "parsed_date": "2024-02", "period_type": "month"}],
    }
    cat = build_catalogue(snap, periods)
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]  # no coverage ids
    facts = [_fact("B9", "2023-01", 0), _fact("B10", "2024-01", 1)]
    links, unmatched, _ = execute_plan(facts, cat, maps, _demand())
    by_cell = {lk.template_cell: lk for lk in links}
    assert by_cell["B9"].source_sheet == "Hist"            # filled via the template's own name
    assert not unmatched


def test_history_filled_by_value_continuity_across_different_labels_and_scale():
    # THE Outlook case: winner "Total sales" (thousands, 2024 only); history sheet
    # "Total revenue" (MILLIONS, 2023-2024) — different label AND different scale.
    # They agree in 2024 at a constant x1000 ratio ⇒ same line ⇒ 2023 fills, with
    # no coverage_series_ids, no shared label, no AI.
    primary = [
        {"row": 1, "col": 1, "value": "EUR thousands", "address": "A1"},
        {"row": 5, "col": 1, "value": "Total sales", "address": "A5"},
        {"row": 5, "col": 3, "value": 12_000_000, "address": "C5"},   # 2024-01
        {"row": 5, "col": 4, "value": 13_000_000, "address": "D5"},   # 2024-02
    ]
    hist = [
        {"row": 1, "col": 1, "value": "EUR millions", "address": "A1"},
        {"row": 5, "col": 1, "value": "Total revenue", "address": "A5"},
        {"row": 5, "col": 3, "value": 10_000.0, "address": "C5"},      # 2023-01 (x1000 smaller)
        {"row": 5, "col": 4, "value": 11_000.0, "address": "D5"},      # 2023-02
        {"row": 5, "col": 5, "value": 12_000.0, "address": "E5"},      # 2024-01  == 12,000,000/1000
        {"row": 5, "col": 6, "value": 13_000.0, "address": "F5"},      # 2024-02  == 13,000,000/1000
    ]
    snap = {"sheets": [{"name": "Primary", "cells": primary}, {"name": "Hist", "cells": hist}]}
    periods = {
        "Primary": [{"col": 3, "parsed_date": "2024-01", "period_type": "month"},
                    {"col": 4, "parsed_date": "2024-02", "period_type": "month"}],
        "Hist": [{"col": 3, "parsed_date": "2023-01", "period_type": "month"},
                 {"col": 4, "parsed_date": "2023-02", "period_type": "month"},
                 {"col": 5, "parsed_date": "2024-01", "period_type": "month"},
                 {"col": 6, "parsed_date": "2024-02", "period_type": "month"}],
    }
    cat = build_catalogue(snap, periods)
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]  # no coverage ids
    facts = [_fact("B9", "2023-01", 0), _fact("B10", "2024-01", 1)]
    links, unmatched, _ = execute_plan(facts, cat, maps, _demand())
    by_cell = {lk.template_cell: lk for lk in links}
    assert by_cell["B9"].source_sheet == "Hist"            # matched purely by value continuity
    assert not unmatched


def _attr_fact(cell):
    # a period-less per-row attribute input (e.g. column C "as-reported name")
    return {"sheet_name": "Template", "cell": cell, "canonical_metric": "revenue",
            "metric_label": "revenue", "unit": None, "currency": None,
            "row": int(cell[1:]), "col": 3, "period_index": None, "scenario": "actual",
            "parsed_date": None, "period_type": None}


def test_attribute_fill_writes_source_label_into_text_slot():
    # Column C wants the SOURCE's name for the line (text), not a number. The
    # period-less text cell gets the mapped series' label written verbatim.
    cat = build_catalogue(_snapshot(), _periods())          # Primary's label is "Revenue"
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]
    facts = [_attr_fact("C10")]
    ctx = ({("Template", "C10"): "General"}, {}, {})        # text-format cell
    links, _u, _i = execute_plan(facts, cat, maps, _demand(), template_context=ctx)
    by_cell = {lk.template_cell: lk for lk in links}
    assert "C10" in by_cell and by_cell["C10"].literal_text == "Revenue"


def test_attribute_fill_skips_numeric_format_cells():
    # A period-less cell with a NUMBER format is not a label slot — never gets text.
    cat = build_catalogue(_snapshot(), _periods())
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]
    facts = [_attr_fact("C10")]
    ctx = ({("Template", "C10"): "#,##0"}, {}, {})          # numeric format
    links, _u, _i = execute_plan(facts, cat, maps, _demand(), template_context=ctx)
    assert not any(lk.literal_text for lk in links)


def test_coverage_completion_never_overwrites_a_primary_fill():
    # When the winner DOES cover the period, the sibling is never consulted.
    cat = build_catalogue(_snapshot(), _periods())
    maps = [MetricMap(metric="revenue", series_id="Primary!r5", confidence=0.9)]
    facts = [_fact("B10", "2024-01", 1)]
    links, _u, _i = execute_plan(facts, cat, maps, _demand())
    assert len(links) == 1 and links[0].source_sheet == "Primary"
