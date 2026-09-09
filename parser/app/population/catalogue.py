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
*means* which template metric; execute reads the actual values from the snapshot.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.population.numfmt import parse_number_format
from app.population.periods import parse_any_date, parse_iso_period
from app.population.units import CCY_TOKENS, Unit, resolve_unit

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




def _letters(col: int) -> str:
    """1-based column index -> Excel letters."""
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


@dataclass
class Series:
    id: str
    sheet: str
    row: int
    label: str
    period_cols: list[tuple[int, date | None, str]]  # (col_index, date, grain)
    unit: Unit
    sample: list[float] = field(default_factory=list)
    # Per-period values keyed "YYYY-MM" (from period_cols dates) — used to prove
    # two differently-labelled series are the SAME line by value continuity
    # (constant ratio across overlapping periods), so cross-sheet history fills
    # even when labels diverge ('Revenue' / 'Total sales' / 'Total revenue').
    by_period: dict = field(default_factory=dict)
    # per-column scenario, keyed by col_index: 'actual' | 'budget' | 'forecast'.
    # Scenario comes from the source's OWN labelling — never from the as-of date.
    # Execute uses it only to honour a template slot that explicitly asks for a
    # non-actual scenario; unset columns default to 'actual'.
    col_scenario: dict[int, str] = field(default_factory=dict)
    # scenario-by-ROW sources (a bare 'Budget' row under each metric row): the
    # variant rows, keyed by scenario. Attached to the METRIC row's series and
    # hidden from the mapper — execute resolves the variant for budget/forecast slots.
    variants: dict[str, "Series"] = field(default_factory=dict)
    # the whole ROW's scenario when tagged (by the AI's per-row judgment or an
    # explicit label); None = untagged/actual-ish.
    scenario: str | None = None
    # TRANSPOSED sheet (periods run DOWN rows, series ACROSS columns): the
    # ``row`` attribute holds the series' COLUMN and each period_cols index is
    # a ROW. Everything that turns (series, period-index) into a real address
    # must go through ``series_cell`` — a real pack in this layout once
    # collapsed to one catalogue entry per sheet and filled 76 of 3,418 cells.
    transposed: bool = False
    # The sheet's fiscal-year START MONTH (1=calendar) inferred from its own
    # period labels ('FY26-Q3' claimed as 2025-12 ⇒ April start). None =
    # no fiscal labels. Execute flags FY-slot fills from non-calendar-fiscal
    # sheets — whether the TEMPLATE's 'FY25' means fiscal FY25 or calendar
    # 2025 is a sponsor convention, never silently assumed.
    fiscal_start: int | None = None


_FY_LABEL = re.compile(r"\bFY\s?(\d{2,4})[\s\-_]?([QM])(\d{1,2})\b", re.IGNORECASE)


def fiscal_start_month(label_dates: list[tuple[str, date | None]]) -> int | None:
    """Infer a sheet's fiscal start month from (period label, claimed date)
    pairs: 'FY26-Q3' ending 2025-12 ⇒ (12 − 3·3) mod 12 + 1 = April. Returns
    the start month only when ≥3 labels agree; 1 = calendar; None = no
    evidence. Pure arithmetic over the sheet's own labels — no convention is
    ever guessed from the company or the template."""
    votes: Counter = Counter()
    for label, d in label_dates:
        if d is None or not isinstance(label, str):
            continue
        m = _FY_LABEL.search(label)
        if not m:
            continue
        n = int(m.group(3))
        offset = 3 * n if m.group(2).upper() == "Q" else n
        votes[(d.month - offset) % 12 + 1] += 1
    if not votes:
        return None
    start, n = votes.most_common(1)[0]
    return start if n >= 3 else None


def series_cell(series: "Series", axis_idx: int) -> str:
    """Real A1 of a series' value at one period-axis index — the ONE
    orientation-aware address builder (classic: axis is a column; transposed:
    axis is a row and series.row holds the column)."""
    if getattr(series, "transposed", False):
        return f"{_letters(series.row)}{axis_idx}"
    return f"{_letters(axis_idx)}{series.row}"


def claim_orientation(periods: list[dict], series: list[dict]) -> str:
    """The geometry of an understanding claim, decided from its OWN cells:
      'columns'  — periods across columns, series in rows (classic)
      'rows'     — periods down rows, series across columns (transposed)
      'unknown'  — inconsistent (e.g. long-format panels) — do NOT catalogue;
                   proceeding once collapsed 27 series into one id, silently.
    Code decides from the claim's geometry; no LLM field is trusted for this."""
    prc = [rc for p in periods if (rc := a1_to_rowcol(p.get("header_cell", "")))]
    src = [rc for s in series if (rc := a1_to_rowcol(s.get("label_cell", "")))]
    if len(prc) < 3:
        return "columns"          # too few period claims to infer — classic default
    pcols, prows = {c for _r, c in prc}, {r for r, _c in prc}
    if len(pcols) >= 3 and len(prows) <= 2:
        axis = "columns"
    elif len(prows) >= 3 and len(pcols) <= 2:
        axis = "rows"
    else:
        return "unknown"
    if len(src) >= 3:             # series must vary along the OTHER axis
        scols, srows = {c for _r, c in src}, {r for r, _c in src}
        varies = len(srows) if axis == "columns" else len(scols)
        if varies < max(2, len(src) // 2):
            return "unknown"
    return axis


_SCEN_WORDS = {"budget": "budget", "bud": "budget",
               "forecast": "forecast", "fcst": "forecast",
               "plan": "forecast", "outlook": "forecast"}
_SCEN_ALT = "budget|forecast|plan|outlook|fcst|bud"
# "Budget (Revenue)" / "Budget - Revenue" / "Budget: Revenue"
_SCEN_FIRST = re.compile(rf"^\s*({_SCEN_ALT})\s*[\(\-–—:]\s*(.+?)\)?\s*$", re.I)
# "Revenue (Budget)" / "Revenue - Budget" / "Revenue: Budget"
_SCEN_LAST = re.compile(rf"^\s*(.+?)\s*[\(\-–—:]\s*({_SCEN_ALT})\s*\)?\s*$", re.I)


def parse_scenario_variant(label: str | None) -> tuple[str | None, str | None]:
    """(scenario, explicit_parent_name) when a row label is an EXPLICIT scenario tag:
      - bare word  ('Budget')            -> ('budget', None)   — variant of the row above
      - compound   ('Budget (Revenue)',
                    'Revenue - Budget')  -> ('budget', 'Revenue') — parent named in the tag
    (None, None) when the label is a normal metric. The tag is the source's own
    labelling — honoured wherever it appears, never inferred."""
    t = (label or "").strip()
    if not t:
        return None, None
    bare = _SCEN_WORDS.get(re.sub(r"[^a-z]", "", t.lower()))
    if bare:
        return bare, None
    m = _SCEN_FIRST.match(t)
    if m:
        return _SCEN_WORDS[m.group(1).lower()], m.group(2).strip()
    m = _SCEN_LAST.match(t)
    if m:
        return _SCEN_WORDS[m.group(2).lower()], m.group(1).strip()
    return None, None


def _norm_label(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _attach_scenario_variant_rows(out: dict[str, "Series"],
                                  ai_tags: dict[str, tuple[str | None, str | None]] | None = None) -> None:
    """Scenario-by-ROW layout: a row that carries another metric's budget/forecast
    figures is attached to that parent series (parent.variants); execute picks the
    right variant per template slot.

    WHO decides is layered (layouts vary too much for rules alone):
      1. the AI's per-row judgment (``ai_tags``: sid -> (scenario, variant_of)) — it
         reads ANY layout: interleaved rows, budget blocks, colour conventions;
      2. the label's own explicit tag ('Budget', 'Budget (Revenue)', 'Revenue -
         Budget') — deterministic validation and the fallback when the AI is silent.
    Parent resolution: the explicitly NAMED metric first, else the metric row above
    (a dataless bare 'Budget' row is a SECTION HEADING, never a variant).

    Guardrail — an LLM tag never hard-gates data: a series is REMOVED from the
    mapper-facing catalogue only when its own label confirms it's a scenario tag;
    an AI-only claim attaches the variant but leaves the row visible to the mapper."""
    ai_tags = ai_tags or {}
    by_sheet: dict[str, list[Series]] = defaultdict(list)
    for s in out.values():
        by_sheet[s.sheet].append(s)
    for ss in by_sheet.values():
        ss.sort(key=lambda s: s.row)

        def _claim(s: Series) -> tuple[str | None, str | None, bool]:
            """(scenario, parent_name, label_confirms) for a row's variant claim."""
            lab_scen, lab_parent = parse_scenario_variant(s.label)
            ai_scen, ai_parent = ai_tags.get(s.id, (None, None))
            if ai_parent or (ai_scen in ("budget", "forecast")):
                return (ai_scen if ai_scen in ("budget", "forecast") else lab_scen,
                        ai_parent or lab_parent, lab_scen is not None)
            return lab_scen, lab_parent, lab_scen is not None

        by_name: dict[str, Series] = {}
        for s in ss:
            scen, pname, _ = _claim(s)
            if scen is None or (pname is None and ai_tags.get(s.id, (None, None))[1] is None
                                and parse_scenario_variant(s.label) == (None, None)):
                by_name.setdefault(_norm_label(s.label), s)
        parent: Series | None = None
        for s in ss:
            scen, pname, label_confirms = _claim(s)
            if scen not in ("budget", "forecast") or (pname is None and not label_confirms):
                # a whole row the AI tagged budget WITHOUT a parent is a row-level
                # scenario tag, not a variant — record it and treat as a metric row.
                if scen and not label_confirms:
                    s.scenario = scen
                parent = s
                continue
            target: Series | None = None
            if pname:                                   # explicitly named parent wins
                target = by_name.get(_norm_label(pname))
            if target is None and parent is not None and s.sample:   # adjacency, data required
                target = parent
            if target is not None and target is not s:
                s.scenario = scen
                s.label = f"{target.label} ({scen})"
                target.variants.setdefault(scen, s)
                if label_confirms:                      # deterministic confirmation -> hide from mapper
                    out.pop(s.id, None)


def normalise_scenario(kind: str | None) -> str:
    """A source column's declared scenario, normalised. Anything the source didn't
    tag as budget/forecast is treated as actuals (management accounts report the
    past by default)."""
    k = (kind or "").strip().lower()
    if k.startswith("budget"):
        return "budget"
    if k.startswith(("forecast", "plan", "outlook")):
        return "forecast"
    return "actual"


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
        for tok, ccy in CCY_TOKENS:
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
        # deterministic detection carries no reliable actual/budget signal — treat
        # every column as actuals so a scenario-agnostic demand fills normally.
        col_scenario = {p["col"]: "actual" for p in raw_periods}
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
            date_by_col = {p["col"]: parse_iso_period(p.get("parsed_date")) for p in raw_periods}
            by_period = {}
            for c in data_cells:
                d = date_by_col.get(c["col"])
                n = _num(effective_value(c))
                if d is not None and n is not None:
                    by_period[f"{d.year:04d}-{d.month:02d}"] = n
            sid = f"{name}!r{row}"
            out[sid] = Series(
                id=sid, sheet=name, row=row, label=label,
                period_cols=period_cols,
                unit=_series_unit(label, number_format, sheet_ccy),
                sample=[v for v in vals if v is not None][:5],
                by_period=by_period,
                col_scenario=col_scenario,
            )
    _attach_scenario_variant_rows(out)
    return out


def _unit_from_llm(unit_str, currency, number_format, sheet_ccy, samples=None) -> Unit:
    """Resolve a series Unit. The AI's EXPLICIT call wins, because it read the
    sheet's meaning; the cell's number-format is only a fallback when the AI is
    unsure. (Trusting the format over the AI was throwing away whole balance sheets:
    money rows the AI correctly tagged 'USD'm' were being overridden to 'percent' by
    a percent-looking format, then blocked by the never-mix-% safety rule.)

    The AI's declared SCALE ("USD'000" -> base=1000) is kept: magnitude
    reconciliation in execute still overrides it whenever the template row holds
    real numbers, but on an EMPTY template row the label math is all there is —
    flattening the base to 1 there wrote thousands-denominated sources in ~1000x
    too small (the 0.01 balance-sheet fills)."""
    from app.population.units import _median_abs
    u = resolve_unit(unit_str)

    # 1) AI explicitly named a money unit (currency and/or a scale word) -> trust it.
    if u.kind == "money":
        return Unit(base=u.base or 1.0, currency=u.currency or currency or sheet_ccy, kind="money")
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


def catalogue_from_understanding(snapshot: dict, sheets: list[dict],
                                 as_of: date | None = None,
                                 diagnostics: list[str] | None = None,
                                 sid_index: dict[tuple[str, str], str] | None = None
                                 ) -> dict[str, "Series"]:
    """Build the catalogue from AI source-understanding instead of deterministic
    detection. `sheets` is a list of {sheet, periods:[{header_cell,date,grain,kind}],
    series:[{label_cell,label,canonical_metric,unit,currency,sign_flip}]}. The AI
    located the structure; we read the real values (cached results) deterministically.

    Every source column is kept and tagged with its own scenario (actual / budget /
    forecast), taken from the source's declared ``kind`` — NOT from the as-of date.
    Scenario is enforced later, in execute, and only when a template slot explicitly
    asks for a non-actual scenario; a budget column can never leak into an actual
    slot because execute matches scenario-to-scenario. ``as_of`` is retained for
    call compatibility (timeline/vintage) but no longer classifies actual vs
    forecast — a time series carries data before and after the as-of alike."""
    _ = as_of  # no longer used to classify scenario (see docstring)
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
    ai_tags: dict[str, tuple[str | None, str | None]] = {}
    for sh in sheets:
        name = sh.get("sheet")
        if name is None:
            continue
        # GEOMETRY FIRST: decide the claim's orientation from its own cells.
        # A transposed sheet processed as classic collapses every series onto
        # one id (all labels share row 4) and every period onto one column —
        # a real pack filled 76/3,418 cells that way, silently.
        orientation = claim_orientation(sh.get("periods", []), sh.get("series", []))
        if orientation == "unknown":
            msg = (f"{name}: claim geometry is inconsistent (long-format panel?) — "
                   f"sheet NOT catalogued rather than collapsed")
            if diagnostics is not None:
                diagnostics.append(msg)
            continue
        transposed = orientation == "rows"
        sheet_ccy = _detect_sheet_currency(cells_by_sheet.get(name, []))
        period_cols: list[tuple[int, date | None, str]] = []
        col_scenario: dict[int, str] = {}
        fy_evidence: list[tuple[str, date | None]] = []
        for p in sh.get("periods", []):
            rc = a1_to_rowcol(p.get("header_cell", ""))
            if not rc:
                continue
            d = parse_iso_period(p.get("date"))
            if d is None:
                # The AI often can't date a formula/complex period header (it returns
                # date=null). Recover it from the header cell's COMPUTED value — a
                # formula date header caches its real date — so a source whose month
                # columns are formula-driven still aligns by date instead of blanking.
                d = parse_any_date(val_by_rc.get((name, rc[0], rc[1])))
            axis = rc[0] if transposed else rc[1]   # period axis: row or column
            # Keep the column whatever its scenario; record the scenario so execute
            # can honour an explicit budget/forecast demand and keep budget out of
            # actual slots. No column is ever dropped here.
            period_cols.append((axis, d, p.get("grain") or "month"))
            col_scenario[axis] = normalise_scenario(p.get("kind"))
            hdr_val = val_by_rc.get((name, rc[0], rc[1]))
            if isinstance(hdr_val, str):
                fy_evidence.append((hdr_val, d))
        if not period_cols:
            continue
        fiscal_start = fiscal_start_month(fy_evidence)
        claimed = 0
        for ser in sh.get("series", []):
            rc = a1_to_rowcol(ser.get("label_cell", ""))
            if not rc:
                continue
            claimed += 1
            anchor = rc[1] if transposed else rc[0]   # series anchor: column or row
            sample = []
            by_period: dict = {}
            for axis, _d, _g in period_cols:
                r_, c_ = (axis, anchor) if transposed else (anchor, axis)
                v = val_by_rc.get((name, r_, c_))
                if _is_number(v):
                    n = _num(v)
                    if n is not None:
                        sample.append(n)
                        if _d is not None:
                            by_period[f"{_d.year:04d}-{_d.month:02d}"] = n
            number_format = next(
                (fmt_by_rc.get((name, axis, anchor) if transposed else (name, anchor, axis))
                 for axis, _, _ in period_cols
                 if fmt_by_rc.get((name, axis, anchor) if transposed else (name, anchor, axis))),
                None)
            sid = f"{name}!{'c' if transposed else 'r'}{anchor}"
            if sid_index is not None:
                # label-cell → sid join for the one-pass mapper: the model
                # references series by the CELL it can see in the grid; only
                # this function knows how a cell becomes an id (orientation).
                sid_index[(name, str(ser.get("label_cell", "")).strip().upper())] = sid
            out[sid] = Series(
                id=sid, sheet=name, row=anchor,
                label=(ser.get("label") or ser.get("canonical_metric") or sid),
                period_cols=period_cols,
                unit=_unit_from_llm(ser.get("unit"), ser.get("currency"), number_format, sheet_ccy, sample),
                sample=sample[:5],
                by_period=by_period,
                col_scenario=col_scenario,
                transposed=transposed,
                fiscal_start=fiscal_start,
            )
            scen = (ser.get("scenario") or "").strip().lower() or None
            vof = (ser.get("variant_of") or "").strip() or None
            if scen or vof:
                ai_tags[sid] = (scen, vof)
        # COLLAPSE BELT: many claims producing few ids is a geometry bug,
        # whatever the orientation call said — say so, never proceed silently.
        produced = sum(1 for sid in out if sid.startswith(f"{name}!"))
        if claimed >= 6 and produced <= max(2, claimed // 3):
            msg = (f"{name}: {claimed} series claims collapsed to {produced} catalogue "
                   f"entries — geometry mismatch, coverage is unreliable")
            if diagnostics is not None:
                diagnostics.append(msg)
    _attach_scenario_variant_rows(out, ai_tags)
    return out
