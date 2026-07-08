"""Deterministic period alignment.

The bad run had the LLM eyeballing which source column was which month — and
interleaving monthly columns with FY/annual columns ('skip the FY col'). Here a
template period slot binds to a source column deterministically (pick_column): by
real date when the template has one, else by newest-anchored position — and only
within the same grain, so a monthly slot can never bind an FY column.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta


def parse_any_date(v) -> date | None:
    """A date from whatever a cell holds: a real date/datetime, an ISO string
    ('2024-01-31T00:00:00'), an Excel serial number, or a period label ('2024-01')."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # Only a realistic Excel date serial (~1980-01-01 .. 2065) counts as a date.
        # Small integers (period indices, counts, 1..43) are NOT dates — treating them
        # as serials produced bogus 1900-era headers that broke timeline detection.
        if 29000 <= v <= 60000:
            return (datetime(1899, 12, 30) + timedelta(days=int(v))).date()
        return None
    if isinstance(v, str):
        s = v.strip()
        for cand in (s, s[:10]):
            try:
                return datetime.fromisoformat(cand).date()
            except ValueError:
                pass
        return parse_iso_period(s)
    return None


def _bucket(d: date, grain: str):
    """Calendar bucket a date falls in, at a given grain — so 31-Jan and 1-Jan
    match for a monthly slot."""
    g = _grain(grain)
    if g == "quarter":
        return (d.year, (d.month - 1) // 3)
    if g == "year":
        return (d.year,)
    return (d.year, d.month)              # default monthly


def infer_grain(dates) -> str | None:
    """Grain of a timeline from the spacing of its dates: ~1 month apart -> month,
    ~3 -> quarter, ~12 -> year. None if it can't be told."""
    ds = sorted(set(d for d in dates if isinstance(d, date)))
    gaps = [(b.year - a.year) * 12 + (b.month - a.month) for a, b in zip(ds, ds[1:])]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    m = Counter(gaps).most_common(1)[0][0]
    return "month" if m <= 1 else "quarter" if m <= 3 else "year"


def _grain(s: str | None) -> str:
    s = (s or "").lower()
    if s.startswith("month"):
        return "month"
    if s.startswith("quarter"):
        return "quarter"
    if s.startswith(("year", "annual")) or s == "fy":
        return "year"
    return s


def parse_iso_period(s: str | None) -> date | None:
    """Parse the ISO-ish period strings detection emits ('YYYY-MM', 'YYYY-Q3',
    'YYYY', 'YYYY-MM-DD') into a comparable date (first of the period)."""
    if not s:
        return None
    s = str(s).strip()
    m = re.fullmatch(r"(\d{4})[\s\-/]?Q([1-4])", s, re.I)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        return date(y, (q - 1) * 3 + 1, 1)
    # 'Q1-26' / 'Q1 2026' / "Q3'25" — quarter-first labels (a template's
    # quarterly dashboard writes them this way; unparsed they'd leave the slot
    # dateless and let positional matching pull a single MONTH into a quarter).
    m = re.fullmatch(r"Q([1-4])[\s\-/'’]*(\d{2}|\d{4})", s, re.I)
    if m:
        q, y = int(m.group(1)), int(m.group(2))
        y += 2000 if y < 100 else 0
        return date(y, (q - 1) * 3 + 1, 1)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), 1)
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return date(int(m.group(1)), 1, 1)
    return None


def _bucket_end(d: date, grain: str) -> tuple[int, int]:
    """(year, month) of the LAST month in the bucket ``d`` falls in."""
    if grain == "quarter":
        return (d.year, ((d.month - 1) // 3) * 3 + 3)
    if grain == "year":
        return (d.year, 12)
    return (d.year, d.month)


def pick_column(period_index: int | None, period_count: int, parsed_date: date | None,
                period_cols: list[tuple[int, date | None, str]], grain: str,
                template_grain: str | None = None, point_in_time: bool = False) -> int | None:
    """Choose the source column for one template period slot.

    period_cols: [(col_index, date, period_type)] for the series' sheet.
    - If the template slot has a real DATE -> match the source column in the same
      calendar bucket at the right grain (31-Jan matches 1-Jan; an FY/LTM column
      never fills a monthly slot). No match -> None (blank, never guessed).
    - POINT-IN-TIME exception: a quarterly/annual STOCK slot (balance sheet,
      debt, headcount) equals its period-END value, so when no same-grain source
      column exists, the MONTH column landing on the bucket's last month
      satisfies it. Never applied to flows — one month is not a quarter of P&L.
    - If the slot has NO date -> align by position, newest-anchored (fallback for
      templates whose columns carry no readable dates).
    """
    def cols_of(gr: str):
        return sorted(((d, c) for (c, d, pt) in period_cols if d is not None and _grain(pt) == gr),
                      key=lambda x: x[0])

    tgr = _grain(template_grain) if template_grain else None
    if tgr in ("month", "quarter", "year"):
        cand_grain, cand = tgr, cols_of(tgr)
    else:
        cand_grain = _grain(grain)
        cand = cols_of(cand_grain)
        if not cand:   # demand grain unusable ("current"/unknown) -> source's dominant real grain
            real = [_grain(pt) for (c, d, pt) in period_cols
                    if d is not None and _grain(pt) in ("month", "quarter", "year")]
            if real:
                cand_grain = Counter(real).most_common(1)[0][0]
                cand = cols_of(cand_grain)

    bgrain = cand_grain if cand_grain in ("month", "quarter", "year") else "month"
    if parsed_date is not None:
        tkey = _bucket(parsed_date, bgrain)
        for d, c in cand:
            if _bucket(d, bgrain) == tkey:
                return c
        if point_in_time and bgrain in ("quarter", "year"):
            end = _bucket_end(parsed_date, bgrain)
            months = cols_of("month")
            for d, c in months:
                if (d.year, d.month) == end:
                    return c
        return None
    if period_index is None or not cand:
        return None
    pos = len(cand) - (period_count - period_index)   # newest template slot -> newest source col
    return cand[pos][1] if 0 <= pos < len(cand) else None
