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
