from datetime import date

from app.raw_extraction.schema import CellInfo, CellType
from app.structure.schema import PeriodStatus
from app.structure.temporal_analyzer import detect_periods

# Fixed "today" far in the future so every 2026 month is unambiguously historical.
TODAY = date(2030, 1, 1)


def _s(addr, row, col, value):
    return CellInfo(address=addr, row=row, col=col, value=value, cell_type=CellType.STRING)


def test_actual_forecast_boundary_sets_current():
    cells = [
        _s("C1", 1, 3, "Actual"), _s("D1", 1, 4, "Actual"), _s("E1", 1, 5, "Actual"),
        _s("F1", 1, 6, "Forecast"),
        _s("C2", 2, 3, "Jan-26"), _s("D2", 2, 4, "Feb-26"),
        _s("E2", 2, 5, "Mar-26"), _s("F2", 2, 6, "Apr-26"),
    ]
    periods = {p.col: p for p in detect_periods(cells, "PL", TODAY)}
    assert periods[5].status == PeriodStatus.CURRENT      # last actual = current
    assert periods[5].label == "Mar-26"
    assert periods[6].status == PeriodStatus.BUDGET        # forecast column


def test_rightmost_historical_fallback():
    cells = [
        _s("C1", 1, 3, "Jan-26"), _s("D1", 1, 4, "Feb-26"), _s("E1", 1, 5, "Mar-26"),
    ]
    periods = {p.col: p for p in detect_periods(cells, "PL", TODAY)}
    # No explicit current → rightmost historical promoted.
    assert periods[5].status == PeriodStatus.CURRENT
    assert periods[3].status == PeriodStatus.HISTORICAL
    assert periods[4].status == PeriodStatus.HISTORICAL


def test_sheet_name_tagged():
    cells = [_s("C1", 1, 3, "Q1 2026")]
    periods = detect_periods(cells, "Covenants", TODAY)
    assert periods and periods[0].sheet_name == "Covenants"


def test_month_and_fy_tokens_require_word_boundaries():
    # "Summary"/"Primary" contain "mar", "Marketing" starts with "mar", "Qualify"
    # ends in "fy" — none of these are period headers.
    for label in ("Summary 2025", "Primary 2024", "Qualify 25", "Marketing 2025"):
        assert detect_periods([_s("C1", 1, 3, label)], "PL", TODAY) == [], label


def test_real_period_labels_still_parse():
    for label, parsed in (("Mar 2025", "2025-03"), ("Mar-25", "2025-03"),
                          ("March 2025", "2025-03"), ("2025 Mar", "2025-03"),
                          ("Sept-25", "2025-09")):
        periods = detect_periods([_s("C1", 1, 3, label)], "PL", TODAY)
        assert periods and periods[0].parsed_date == parsed, label
    fy = detect_periods([_s("C1", 1, 3, "FY25")], "PL", TODAY)
    assert fy and fy[0].parsed_date == "2025" and fy[0].period_type == "year"


def test_single_current_when_boundary_and_fallback_disagree():
    # Explicit Actual marker on Jan only; Forecast from Mar. Promoting the
    # rightmost historical (Feb) BEFORE the boundary pass used to leave two
    # CURRENT columns. The explicit Actual→Forecast boundary wins.
    cells = [
        _s("C1", 1, 3, "Actual"), _s("E1", 1, 5, "Forecast"),
        _s("C2", 2, 3, "Jan-26"), _s("D2", 2, 4, "Feb-26"), _s("E2", 2, 5, "Mar-26"),
    ]
    periods = {p.col: p for p in detect_periods(cells, "PL", TODAY)}
    currents = [p for p in periods.values() if p.status == PeriodStatus.CURRENT]
    assert len(currents) == 1 and currents[0].col == 3
    assert periods[4].status == PeriodStatus.HISTORICAL


def test_year_slot_bridges_from_quarterly_source():
    # quarterly-only BS source serving a template FY column — the WTAF gap:
    # 'end' takes the final quarter's balance, 'sum' needs all four quarters
    from datetime import date as _d
    from app.population.periods import align_slot
    q = [(3, _d(2026, 3, 31), "quarter"), (4, _d(2026, 6, 30), "quarter"),
         (5, _d(2026, 9, 30), "quarter"), (6, _d(2026, 12, 31), "quarter")]
    picked, why = align_slot(0, 1, _d(2026, 12, 31), q, "month",
                             template_grain="year", rollup="end")
    assert picked == ([6], "single") and why is None          # Q4 balance
    picked, why = align_slot(0, 1, _d(2026, 12, 31), q, "month",
                             template_grain="year", rollup="sum")
    assert picked == ([3, 4, 5, 6], "sum") and why is None    # four-quarter flow
    picked, why = align_slot(0, 1, _d(2026, 12, 31), q[:3], "month",
                             template_grain="year", rollup="sum")
    assert picked is None and why == "bucket_incomplete"      # 3 of 4 quarters
    picked, why = align_slot(0, 1, _d(2026, 12, 31), q[:3], "month",
                             template_grain="year", rollup="end")
    assert picked is None and why == "period_end_missing"     # Q4 absent


def test_incomplete_months_never_fall_through_to_quarters():
    # months present-but-incomplete DECIDE (bucket_incomplete); mixing grains
    # inside one bucket would double-count
    from datetime import date as _d
    from app.population.periods import align_slot
    cols = ([(c, _d(2026, m, 28), "month") for c, m in ((10, 1), (11, 2), (12, 3))]
            + [(3, _d(2026, 3, 31), "quarter"), (4, _d(2026, 6, 30), "quarter"),
               (5, _d(2026, 9, 30), "quarter"), (6, _d(2026, 12, 31), "quarter")])
    picked, why = align_slot(0, 1, _d(2026, 12, 31), cols, "month",
                             template_grain="year", rollup="sum")
    assert picked is None and why == "bucket_incomplete"
