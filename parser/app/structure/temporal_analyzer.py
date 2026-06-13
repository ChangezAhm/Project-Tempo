"""Temporal analysis: parse period headers, classify historical/current/future.

Ported from template-compiler-prototype/src/parsing/temporal_analyzer.py
(algorithm unchanged). Emits the lean structure.DetectedPeriod.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from app.raw_extraction.schema import CellInfo, CellType
from app.structure.schema import DetectedPeriod, PeriodStatus

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}

_MONTH_YR = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s\-_./]?(\d{2,4})",
    re.IGNORECASE,
)
_YR_MONTH = re.compile(
    r"(\d{4})[\s\-_./](jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*",
    re.IGNORECASE,
)
_QUARTER = re.compile(r"Q([1-4])[\s\-_./]?(\d{2,4})", re.IGNORECASE)
_YR_QUARTER = re.compile(r"(\d{4})[\s\-_./]?Q([1-4])", re.IGNORECASE)
_FY = re.compile(r"FY[\s\-_]?(\d{2,4})", re.IGNORECASE)
_YTD = re.compile(r"\bYTD\b|year[\s\-]to[\s\-]date", re.IGNORECASE)
_LTM = re.compile(r"\bLTM\b|\bTTM\b|last twelve|trailing twelve", re.IGNORECASE)
_BUDGET = re.compile(r"\bbudget\b|\bforecast\b|\bplan\b|\btarget\b", re.IGNORECASE)
_ACTUAL = re.compile(r"\bactual\b", re.IGNORECASE)

_HEADER_SCAN_ROWS = 12


def detect_periods(
    cells: list[CellInfo],
    sheet_name: str,
    today: date | None = None,
) -> list[DetectedPeriod]:
    if today is None:
        today = date.today()

    periods: list[DetectedPeriod] = []
    seen_cols: set[int] = set()

    # Pass 1: textual labels in header rows
    for c in cells:
        if c.row > _HEADER_SCAN_ROWS or c.col <= 2:
            continue
        if c.col in seen_cols:
            continue
        if isinstance(c.value, str) and c.value.strip():
            period = _parse_period_label(c.value.strip(), c.col, today)
            if period:
                periods.append(period)
                seen_cols.add(c.col)
        elif c.cell_type == CellType.DATE and c.value:
            try:
                dt = _parse_date_value(c.value)
                if dt:
                    periods.append(DetectedPeriod(
                        col=c.col,
                        label=str(c.value),
                        parsed_date=dt.strftime("%Y-%m"),
                        period_type="month",
                        status=_classify_month(dt.year, dt.month, today),
                    ))
                    seen_cols.add(c.col)
            except Exception:
                pass

    # Pass 2: column-type markers (Actual / Forecast / Budget / Plan)
    actual_cols: set[int] = set()
    forecast_cols: set[int] = set()
    budget_cols: set[int] = set()
    for c in cells:
        if c.row > _HEADER_SCAN_ROWS or c.col <= 2:
            continue
        if not isinstance(c.value, str):
            continue
        label = c.value.strip().lower()
        if not label:
            continue
        if _ACTUAL.search(label):
            actual_cols.add(c.col)
        if _BUDGET.search(label):
            budget_cols.add(c.col)
            forecast_cols.add(c.col)

    for p in periods:
        if p.col in budget_cols and p.status != PeriodStatus.BUDGET:
            p.status = PeriodStatus.BUDGET
        elif p.col in actual_cols and p.status == PeriodStatus.UNKNOWN:
            p.status = PeriodStatus.HISTORICAL

    periods.sort(key=lambda p: p.col)

    # Fallback: no CURRENT → promote the rightmost historical (most recent).
    has_current = any(p.status == PeriodStatus.CURRENT for p in periods)
    if not has_current and periods:
        historicals = [p for p in periods if p.status == PeriodStatus.HISTORICAL]
        if historicals:
            historicals[-1].status = PeriodStatus.CURRENT
        else:
            non_budget = [p for p in periods if p.status != PeriodStatus.BUDGET]
            if non_budget:
                non_budget[-1].status = PeriodStatus.CURRENT

    # Actual→Forecast boundary: last actual before first forecast = current.
    if actual_cols and (forecast_cols or budget_cols):
        last_actual = max(actual_cols)
        first_forecast = min(forecast_cols | budget_cols)
        if last_actual < first_forecast:
            for p in periods:
                if p.col == last_actual and p.status != PeriodStatus.BUDGET:
                    p.status = PeriodStatus.CURRENT

    for p in periods:
        p.sheet_name = sheet_name
    return periods


def _parse_period_label(label: str, col: int, today: date) -> DetectedPeriod | None:
    label_stripped = label.strip()

    if _YTD.search(label_stripped):
        return DetectedPeriod(col=col, label=label_stripped, period_type="ytd", status=PeriodStatus.YTD)
    if _LTM.search(label_stripped):
        return DetectedPeriod(col=col, label=label_stripped, period_type="ltm", status=PeriodStatus.LTM)
    if _BUDGET.search(label_stripped):
        parsed, _ = _extract_date_from_label(label_stripped)
        return DetectedPeriod(col=col, label=label_stripped, parsed_date=parsed,
                              period_type="budget", status=PeriodStatus.BUDGET)

    m = _MONTH_YR.search(label_stripped)
    if m:
        month = _MONTHS.get(m.group(1).lower()[:3])
        year = _normalize_year(m.group(2))
        if month and year:
            return DetectedPeriod(col=col, label=label_stripped,
                                  parsed_date=f"{year:04d}-{month:02d}",
                                  period_type="month", status=_classify_month(year, month, today))

    m = _YR_MONTH.search(label_stripped)
    if m:
        year = int(m.group(1))
        month = _MONTHS.get(m.group(2).lower()[:3])
        if month:
            return DetectedPeriod(col=col, label=label_stripped,
                                  parsed_date=f"{year:04d}-{month:02d}",
                                  period_type="month", status=_classify_month(year, month, today))

    m = _QUARTER.search(label_stripped)
    if m:
        q = int(m.group(1))
        year = _normalize_year(m.group(2))
        if year:
            return DetectedPeriod(col=col, label=label_stripped,
                                  parsed_date=f"{year:04d}-Q{q}",
                                  period_type="quarter", status=_classify_quarter(year, q, today))

    m = _YR_QUARTER.search(label_stripped)
    if m:
        year = int(m.group(1))
        q = int(m.group(2))
        return DetectedPeriod(col=col, label=label_stripped,
                              parsed_date=f"{year:04d}-Q{q}",
                              period_type="quarter", status=_classify_quarter(year, q, today))

    m = _FY.search(label_stripped)
    if m:
        year = _normalize_year(m.group(1))
        if year:
            status = (PeriodStatus.HISTORICAL if year < today.year
                      else PeriodStatus.CURRENT if year == today.year
                      else PeriodStatus.FUTURE)
            return DetectedPeriod(col=col, label=label_stripped, parsed_date=f"{year:04d}",
                                  period_type="year", status=status)

    return None


def _extract_date_from_label(label: str) -> tuple[str | None, str]:
    m = _MONTH_YR.search(label)
    if m:
        month = _MONTHS.get(m.group(1).lower()[:3])
        year = _normalize_year(m.group(2))
        if month and year:
            return f"{year:04d}-{month:02d}", "month"
    m = _QUARTER.search(label)
    if m:
        year = _normalize_year(m.group(2))
        if year:
            return f"{year:04d}-Q{m.group(1)}", "quarter"
    return None, ""


def _normalize_year(yr_str: str) -> int | None:
    try:
        yr = int(yr_str)
    except ValueError:
        return None
    if yr < 100:
        yr += 2000
    if yr < 1990 or yr > 2050:
        return None
    return yr


def _classify_month(year: int, month: int, today: date) -> PeriodStatus:
    period_date = date(year, month, 1)
    current_month_start = date(today.year, today.month, 1)
    if today.month == 1:
        prev_month = date(today.year - 1, 12, 1)
    else:
        prev_month = date(today.year, today.month - 1, 1)

    if period_date < prev_month:
        return PeriodStatus.HISTORICAL
    elif period_date == prev_month:
        return PeriodStatus.CURRENT
    elif period_date == current_month_start:
        return PeriodStatus.CURRENT
    else:
        return PeriodStatus.FUTURE


def _classify_quarter(year: int, quarter: int, today: date) -> PeriodStatus:
    today_q = (today.month - 1) // 3 + 1
    if today_q == 1:
        prev_year, prev_q = today.year - 1, 4
    else:
        prev_year, prev_q = today.year, today_q - 1

    period_key = year * 10 + quarter
    current_key = today.year * 10 + today_q
    prev_key = prev_year * 10 + prev_q

    if period_key < prev_key:
        return PeriodStatus.HISTORICAL
    if period_key == prev_key or period_key == current_key:
        return PeriodStatus.CURRENT
    return PeriodStatus.FUTURE


def _parse_date_value(val) -> datetime | None:
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    return None
