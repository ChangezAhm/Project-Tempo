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
