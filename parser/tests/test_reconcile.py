"""Coverage reconciler: code audits the AI's source-understanding claims against
snapshot facts — the March-26 truncation incident as a permanent regression."""

from app.population.reconcile import reconcile_source_understanding


def _cell(addr, row, col, value):
    return {"address": addr, "row": row, "col": col, "value": value}


def _snapshot():
    """12 dated columns (Jan..Dec 2026), a basis row tagging Oct-Dec 'Forecast',
    and two data rows — one of which the claim will 'forget'."""
    cells = [_cell("A6", 6, 1, "Period ending"), _cell("A7", 7, 1, "Reporting basis"),
             _cell("B10", 10, 2, "Revenue"), _cell("B11", 11, 2, "Headcount")]
    for i in range(12):
        col = 3 + i
        letter = chr(ord("C") + i)
        cells.append(_cell(f"{letter}6", 6, col, f"2026-{i + 1:02d}-28"))
        cells.append(_cell(f"{letter}7", 7, col, "Forecast" if i >= 9 else "Actual"))
        cells.append(_cell(f"{letter}10", 10, col, 100.0 + i))
        cells.append(_cell(f"{letter}11", 11, col, 40 + i))
    return {"sheets": [{"name": "MA", "cells": cells}]}


def _claim():
    """The AI saw only Jan..Jun, untagged, and only the Revenue row."""
    return [{"sheet": "MA",
             "periods": [{"header_cell": f"{chr(ord('C') + i)}6",
                          "date": f"2026-{i + 1:02d}-28", "grain": "month",
                          "kind": "actual"} for i in range(6)],
             "series": [{"label_cell": "B10", "label": "Revenue"}]}]


def test_missing_period_columns_are_added_from_date_header():
    sheets, report = reconcile_source_understanding(_snapshot(), _claim())
    periods = sheets[0]["periods"]
    assert len(periods) == 12                      # 6 claimed + 6 recovered
    assert report["MA"]["period_cols_added"] == 6
    assert report["MA"]["timeline"] == "2026-01..2026-12"
    dates = sorted(p["date"] for p in periods)
    assert dates[-1] == "2026-12-28"


def test_basis_row_scenario_overrides_and_fills_kind():
    sheets, report = reconcile_source_understanding(_snapshot(), _claim())
    kind_by_date = {p["date"]: p.get("kind") for p in sheets[0]["periods"]}
    assert kind_by_date["2026-10-28"] == "forecast"    # recovered col, tagged
    assert kind_by_date["2026-12-28"] == "forecast"
    assert kind_by_date["2026-02-28"] == "actual"      # basis row confirms claim
    assert report["MA"]["scenario_tags_set"] >= 3


def test_unclaimed_numeric_rows_become_series():
    sheets, report = reconcile_source_understanding(_snapshot(), _claim())
    labels = {s["label"] for s in sheets[0]["series"]}
    assert "Headcount" in labels                   # the forgotten row recovered
    assert report["MA"]["series_added"] == 1
    cells = {s["label_cell"] for s in sheets[0]["series"] if s["label"] == "Headcount"}
    assert cells == {"B11"}


def test_complete_claim_is_untouched():
    snap = _snapshot()
    sheets, _ = reconcile_source_understanding(snap, _claim())
    sheets2, report2 = reconcile_source_understanding(snap, sheets)
    assert report2 == {}                           # idempotent: nothing to patch
    assert len(sheets2[0]["periods"]) == 12


def test_mixed_actual_forecast_basis_tags_forecast_budget_mix_stays_untagged():
    snap = _snapshot()
    # an FY-style blended column ('Actual / Forecast') embeds projections —
    # tagged forecast; a budget/forecast mix is genuinely ambiguous — untagged
    snap["sheets"][0]["cells"] += [
        _cell("P6", 6, 16, "2026-12-31"), _cell("P7", 7, 16, "Actual / Forecast"),
        _cell("P10", 10, 16, 1200.0), _cell("P11", 11, 16, 45.0),
        _cell("Q6", 6, 17, "2027-12-31"), _cell("Q7", 7, 17, "Budget / Forecast"),
        _cell("Q10", 10, 17, 1300.0), _cell("Q11", 11, 17, 46.0)]
    sheets, _ = reconcile_source_understanding(snap, _claim())
    by_date = {p["date"]: p.get("kind") for p in sheets[0]["periods"]}
    assert by_date["2026-12-31"] == "forecast"
    assert by_date["2027-12-31"] is None
