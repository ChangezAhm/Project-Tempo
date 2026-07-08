"""Deterministic SOURCE catalogue.

The bad run sent the LLM an *image* of the source and asked it to read the
numbers, guess A1 addresses, and guess per-cell scale — which is exactly where the
hallucinated/1000x values came from. Aspose already knows the facts, so we build
the source inventory deterministically here:

  a SERIES = one labelled row in a source sheet that carries numbers across that
  sheet's detected period columns. e.g. "Revenue" on 'P&L' across Jan..Dec.

Each series records its sheet, row, label, the period columns (col index + real
date + grain), and a resolved Unit (scale + currency) inferred from the label and
sheet — never from the LLM. The LLM's only job downstream is to say which series
*means* which template metric; binding reads the actual values from the snapshot.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.population.numfmt import parse_number_format
from app.population.periods import parse_iso_period
from app.population.units import Unit, resolve_unit

_A1 = re.compile(r"^([A-Za-z]{1,3})(\d+)$")


def a1_to_rowcol(addr: str) -> tuple[int, int] | None:
    """'AD20' -> (row=20, col=30) (col 1-based). None if not a plain A1 ref."""
    m = _A1.match((addr or "").strip())
    if not m:
        return None
    letters, row = m.group(1).upper(), int(m.group(2))
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return row, col

_CCY_TOKENS = {
    "$": "USD", "usd": "USD", "us$": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
}


@dataclass
class Series:
    id: str
    sheet: str
    row: int
    label: str
    period_cols: list[tuple[int, date | None, str]]  # (col_index, date, period_type)
    unit: Unit
    sample: list[float] = field(default_factory=list)


def effective_value(cell: dict):
    """The cell's real value. For a formula cell, snapshot `value` holds the formula
    TEXT ('=EOMONTH(...)') and the computed result is in `cached_value` — so most
    real source data (formula-driven flash packs, models) is invisible if you read
    `value`. Prefer a numeric `value`, else the computed `cached_value`, else the
    text."""
    v = cell.get("value")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    cv = cell.get("cached_value")
    if isinstance(v, str) and v.startswith("=") and cv is not None:
        return cv
    if (v is None or v == "") and cv is not None:
        return cv
    return v


def _is_number(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        t = v.strip().replace(",", "").replace("(", "-").replace(")", "").replace("%", "")
        if not t:
            return False
        try:
            float(t)
            return True
        except ValueError:
            return False
    return False


def _num(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str) and _is_number(v):
        t = v.strip().replace(",", "")
        neg = t.startswith("(") and t.endswith(")")
        t = t.replace("(", "").replace(")", "").replace("%", "")
        try:
            f = float(t)
            return -f if neg else f
        except ValueError:
            return None
    return None


def _detect_sheet_currency(cells: list[dict]) -> str | None:
    """Majority currency token seen in text cells on the sheet (a sheet usually
    declares its currency once, in a header)."""
    tally: dict[str, int] = defaultdict(int)
    for c in cells:
        v = effective_value(c)
        if not isinstance(v, str):
            continue
        low = v.lower()
        for tok, ccy in _CCY_TOKENS.items():
            if tok in low:
                tally[ccy] += 1
    return max(tally, key=tally.get) if tally else None


def _row_label(row_cells: list[dict]) -> str | None:
    """Leftmost non-empty text cell in the row = the metric label."""
    text = []
    for c in row_cells:
        v = effective_value(c)
        if isinstance(v, str) and v.strip() and not _is_number(v):
            text.append((c["col"], v.strip()))
    if not text:
        return None
    return min(text, key=lambda x: x[0])[1]


def _series_unit(label: str, number_format: str | None, sheet_ccy: str | None) -> Unit:
    """Resolve a series' unit, trusting the number FORMAT for kind/currency (it's
    deterministic) over the label (which is a mess). Scale is intentionally left as
    raw ones (base=1) for money — the source stores true values; the real scale is
    decided later by magnitude reconciliation, not by labels."""
    fmt = parse_number_format(number_format)
    lab = resolve_unit(label)

    # percent / ratio: the format is authoritative; label as a backup
    if fmt.kind in ("percent", "ratio"):
        return fmt
    if lab.kind in ("percent", "ratio"):
        return lab

    currency = lab.currency or fmt.currency or sheet_ccy
    return Unit(base=1.0, currency=currency, kind="money")


def build_catalogue(snapshot: dict,
                    periods_by_sheet: dict[str, list[dict]]) -> dict[str, Series]:
    """snapshot: source workbook snapshot (sheets[].cells[] with row/col/value).
    periods_by_sheet: {sheet_name: [{col:int, parsed_date:str|None, period_type:str}]}
    from detection. Returns {series_id: Series}."""
    out: dict[str, Series] = {}
    for sheet in snapshot.get("sheets", []):
        name = sheet.get("name")
        raw_periods = periods_by_sheet.get(name) or []
        if not raw_periods:
            continue
        period_cols = [(p["col"], parse_iso_period(p.get("parsed_date")), p.get("period_type", ""))
                       for p in raw_periods]
        pcol_idx = {p["col"] for p in raw_periods}
        cells = sheet.get("cells", [])
        sheet_ccy = _detect_sheet_currency(cells)

        by_row: dict[int, list[dict]] = defaultdict(list)
        for c in cells:
            by_row[c["row"]].append(c)

        for row, rcs in by_row.items():
            label = _row_label(rcs)
            if not label:
                continue
            data_cells = [c for c in rcs if c["col"] in pcol_idx and _is_number(effective_value(c))]
            if not data_cells:
                continue
            vals = [_num(effective_value(c)) for c in data_cells]
            number_format = next(((c.get("style") or {}).get("number_format")
                                  for c in data_cells if (c.get("style") or {}).get("number_format")), None)
            sid = f"{name}!r{row}"
            out[sid] = Series(
                id=sid, sheet=name, row=row, label=label,
                period_cols=period_cols,
                unit=_series_unit(label, number_format, sheet_ccy),
                sample=[v for v in vals if v is not None][:5],
            )
    return out


def _unit_from_llm(unit_str, currency, number_format, sheet_ccy, samples=None) -> Unit:
    """Resolve a series Unit. The AI's EXPLICIT call wins, because it read the
    sheet's meaning; the cell's number-format is only a fallback when the AI is
    unsure. (Trusting the format over the AI was throwing away whole balance sheets:
    money rows the AI correctly tagged 'USD'm' were being overridden to 'percent' by
    a percent-looking format, then blocked by the never-mix-% safety rule.)

    Scale stays raw (base=1) — the real scale is decided by magnitude reconciliation
    in binding, never here."""
    from app.population.units import _median_abs
    u = resolve_unit(unit_str)

    # 1) AI explicitly named a money unit (currency and/or a scale word) -> trust it.
    if u.kind == "money":
        return Unit(base=1.0, currency=u.currency or currency or sheet_ccy, kind="money")
    # 2) AI explicitly said percent / ratio -> trust it.
    if u.kind in ("percent", "ratio"):
        return u

    # 3) AI gave no usable unit -> fall back to the number format (with a magnitude
    #    sanity check: a 'percent' whose values are clearly money-sized isn't one).
    fmt = parse_number_format(number_format)
    if fmt.kind in ("percent", "ratio"):
        med = _median_abs(samples or [])
        if med is None or med <= 100:
            return fmt
    ccy = (currency or None) or fmt.currency or sheet_ccy
    return Unit(base=1.0, currency=ccy, kind="money")


def catalogue_from_understanding(snapshot: dict, sheets: list[dict]) -> dict[str, "Series"]:
    """Build the catalogue from AI source-understanding instead of deterministic
    detection. `sheets` is a list of {sheet, periods:[{header_cell,date,grain}],
    series:[{label_cell,label,canonical_metric,unit,currency,sign_flip}]}. The AI
    located the structure; we read the real values (cached results) deterministically."""
    val_by_rc: dict[tuple[str, int, int], object] = {}
    fmt_by_rc: dict[tuple[str, int, int], str | None] = {}
    cells_by_sheet: dict[str, list[dict]] = defaultdict(list)
    for s in snapshot.get("sheets", []):
        nm = s.get("name")
        for c in s.get("cells", []):
            cells_by_sheet[nm].append(c)
            val_by_rc[(nm, c["row"], c["col"])] = effective_value(c)
            fmt_by_rc[(nm, c["row"], c["col"])] = (c.get("style") or {}).get("number_format")

    out: dict[str, Series] = {}
    for sh in sheets:
        name = sh.get("sheet")
        if name is None:
            continue
        sheet_ccy = _detect_sheet_currency(cells_by_sheet.get(name, []))
        period_cols: list[tuple[int, date | None, str]] = []
        for p in sh.get("periods", []):
            rc = a1_to_rowcol(p.get("header_cell", ""))
            if not rc:
                continue
            period_cols.append((rc[1], parse_iso_period(p.get("date")), p.get("grain") or "month"))
        if not period_cols:
            continue
        for ser in sh.get("series", []):
            rc = a1_to_rowcol(ser.get("label_cell", ""))
            if not rc:
                continue
            row = rc[0]
            sample = []
            for col, _d, _g in period_cols:
                v = val_by_rc.get((name, row, col))
                if _is_number(v):
                    n = _num(v)
                    if n is not None:
                        sample.append(n)
            number_format = next((fmt_by_rc.get((name, row, col)) for col, _, _ in period_cols
                                  if fmt_by_rc.get((name, row, col))), None)
            sid = f"{name}!r{row}"
            out[sid] = Series(
                id=sid, sheet=name, row=row,
                label=(ser.get("label") or ser.get("canonical_metric") or sid),
                period_cols=period_cols,
                unit=_unit_from_llm(ser.get("unit"), ser.get("currency"), number_format, sheet_ccy, sample),
                sample=sample[:5],
            )
    return out
