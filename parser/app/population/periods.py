"""Deterministic period alignment.

The bad run had the LLM eyeballing which source column was which month — and
interleaving monthly columns with FY/annual columns ('skip the FY col'). Here a
template period slot binds to a source column deterministically (align_slot): by
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


def sheet_grains(dates_by_col: dict[tuple[str, int], date]) -> dict[str, str | None]:
    """Per-sheet timeline grain from a {(sheet, col): date} map (the template
    context's own column dates) — the shared shape verify/execute consume."""
    by_sheet: dict[str, list[date]] = {}
    for (sh, _c), d in dates_by_col.items():
        by_sheet.setdefault(sh, []).append(d)
    return {sh: infer_grain(ds) for sh, ds in by_sheet.items()}


def _grain(s: str | None) -> str:
    s = (s or "").lower()
    if s.startswith("month"):
        return "month"
    if s.startswith("quarter"):
        return "quarter"
    if s.startswith(("year", "annual")) or s == "fy":
        return "year"
    return s


_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_iso_period(s: str | None) -> date | None:
    """Parse the ISO-ish period strings detection emits ('YYYY-MM', 'YYYY-Q3',
    'YYYY', 'YYYY-MM-DD') into a comparable date (first of the period)."""
    if not s:
        return None
    s = str(s).strip()
    # 'Jan-25' / 'Jan 2025' / "Sept'25" — month-name timeline labels. Only an
    # EXACT month name/abbreviation counts ('Margin-25' must not read as March);
    # unparsed these leave a whole sheet dateless and force positional matching.
    m = re.fullmatch(r"([A-Za-z]{3,9})[\s\-/'’.]*(\d{2}|\d{4})", s)
    if m and m.group(1).lower() in _MONTH_NAMES:
        y = int(m.group(2))
        y += 2000 if y < 100 else 0
        return date(y, _MONTH_NAMES[m.group(1).lower()], 1)
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


def align_slot(period_index: int | None, period_count: int, parsed_date: date | None,
               period_cols: list[tuple[int, date | None, str]], grain: str,
               template_grain: str | None = None, rollup: str | None = None,
               ) -> tuple[tuple[list[int], str] | None, str | None]:
    """Choose the source column(s) for one template period slot — pure EXECUTION
    of declared semantics, with a typed reason when nothing fits.

    Returns ((columns, op), None) on success — op 'single' (one column holds the
    value) or 'sum'/'avg' (aggregate the columns' values) — or (None, code):

      no_source_periods     the series has no period columns at all
      positional_out_of_range  dateless alignment fell off the timeline
      no_slot_index         the slot has neither a date nor an index
      no_column_in_bucket   dated slot, no source column lands in its bucket
      grain_unbridgeable    finer columns exist in the bucket but no rollup op
                            was declared — never guessed (a summed stock would 3x)
      bucket_incomplete     sum/avg rollup needs the COMPLETE bucket (3/12 months)
      period_end_missing    'end' rollup: the bucket's last month isn't in the source

    period_cols: [(col_index, date, period_type)] for the series' sheet.
    - A real template DATE matches the source column in the same calendar bucket
      at the right grain (31-Jan matches 1-Jan; an FY/LTM column never fills a
      monthly slot).
    - CROSS-GRAIN ROLLUP: a quarterly/annual slot whose bucket holds only MONTH
      columns is computable from them per the declared ``rollup`` op.
    - A slot with NO date aligns by position, newest-anchored (for templates
      whose columns carry no readable dates); the caller flags such fills."""
    def cols_of(gr: str):
        return sorted(((d, c) for (c, d, pt) in period_cols if d is not None and _grain(pt) == gr),
                      key=lambda x: x[0])

    if not period_cols:
        return None, "no_source_periods"

    # SOURCE fully undated: the understanding couldn't date ANY column (e.g. formula/
    # complex headers it couldn't read). There's nothing to match on, so align
    # POSITIONALLY by column order, newest-anchored — the only sane option, and the
    # caller flags such fills so a human verifies the alignment.
    if not any(d is not None for (_c, d, _pt) in period_cols):
        if period_index is None:
            return None, "no_slot_index"
        cols = sorted(c for (c, _d, _pt) in period_cols)
        pos = len(cols) - (period_count - period_index)
        if 0 <= pos < len(cols):
            return ([cols[pos]], "single"), None
        return None, "positional_out_of_range"

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
                return ([c], "single"), None
        if bgrain in ("quarter", "year"):
            # CROSS-GRAIN BRIDGE, generalized: a coarse slot fills from the
            # FINEST sub-grain present in its bucket — months first, then (for
            # a year slot) quarters. The finest-present grain DECIDES: an
            # incomplete month set never falls through to quarters (mixing
            # grains inside one bucket double-counts). A quarterly-only BS
            # source can thus serve FY columns (final-quarter balance for
            # 'end' stocks, four-quarter sum for flows) — previously only
            # months bridged and quarterly sources left every annual slot blank.
            sub_grains = [("month", 3 if bgrain == "quarter" else 12)]
            if bgrain == "year":
                sub_grains.append(("quarter", 4))
            for fgrain, need in sub_grains:
                units, seen = [], set()
                for d, c in cols_of(fgrain):
                    if _bucket(d, bgrain) == tkey and _bucket(d, fgrain) not in seen:
                        seen.add(_bucket(d, fgrain))
                        units.append((d, c))
                if not units:
                    continue                      # try the next finer grain
                if rollup == "end":
                    if fgrain == "month":
                        end_key = _bucket_end(parsed_date, bgrain)
                        keys = [((d.year, d.month), c) for d, c in units]
                    else:                         # final quarter of the year
                        end_key = (tkey[0], 3)
                        keys = [(_bucket(d, "quarter"), c) for d, c in units]
                    for key, c in keys:
                        if key == end_key:
                            return ([c], "single"), None
                    return None, "period_end_missing"
                if rollup in ("sum", "avg"):
                    if len(units) == need:
                        return ([c for _d, c in units], rollup), None
                    return None, "bucket_incomplete"
                return None, "grain_unbridgeable"
            return None, "no_column_in_bucket"
        return None, "no_column_in_bucket"
    if period_index is None:
        return None, "no_slot_index"
    if not cand:
        return None, "no_column_in_bucket"
    pos = len(cand) - (period_count - period_index)   # newest template slot -> newest source col
    if 0 <= pos < len(cand):
        return ([cand[pos][1]], "single"), None
    return None, "positional_out_of_range"
