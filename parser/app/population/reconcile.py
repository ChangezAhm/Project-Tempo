"""Deterministic audit of the AI's source understanding — code checks the AI.

The source understanding is one LLM pass, and a single incomplete answer used to
become the unquestioned truth about the source: a real run reported a sheet's
timeline as ending in March-2026 when the sheet carried dated columns through
December-2026 with its own "Actual/Forecast" basis row — every later month, every
forecast slot, silently unfillable.

The snapshot already holds the facts that expose such a miss: the date-header
row, the basis row, and which rows carry numbers. This module reconciles the
CLAIM against those FACTS and patches it:

  - period columns: every column whose header cell parses to a real date is a
    period column — missing ones are ADDED to the claim.
  - scenario kind: a basis row ("Actual"/"Budget"/"Forecast" under the dates) is
    the source's own declaration — where it is unambiguous it OVERRIDES the
    claim's kind; the claim fills the gaps.
  - series: a labelled row with numbers across the period columns is data —
    unclaimed ones are ADDED (label + label_cell only; meaning stays with the
    mapper).

Nothing here is silent: the returned report says exactly what was patched, and
the caller surfaces it in the run's routing block. Pure — no I/O, no LLM.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from app.population.catalogue import (
    _is_number, _row_label, a1_to_rowcol, effective_value,
)
from app.population.periods import parse_any_date
from app.raw_extraction.column_utils import column_letter

logger = logging.getLogger(__name__)

import re as _re

_ACTUAL_W = {"actual", "actuals", "act"}
_BUDGET_W = {"budget", "bud"}
_FORECAST_W = {"forecast", "fcst", "plan", "outlook"}


_COMPARISON_W = {"vs", "versus", "variance", "var", "delta", "diff", "bridge", "walk"}


def _scen_of(v) -> str | None:
    """A basis-row cell's scenario, or None when absent/ambiguous. A MIXED
    'Actual / Forecast' basis (a blended full-year column: elapsed months
    actual, remainder projected) tags FORECAST — the column embeds projections,
    and calling it pure actuals is the more dangerous misread. Budget mixed
    with anything stays untagged (genuinely ambiguous), and a COMPARISON label
    ('Actual vs Forecast variance') is a derived column, never a scenario tag —
    tagging it would route variance numbers into forecast slots."""
    if not isinstance(v, str):
        return None
    words = set(_re.findall(r"[a-z]+", v.strip().lower()))
    if words & _COMPARISON_W:
        return None
    has_a, has_b, has_f = words & _ACTUAL_W, words & _BUDGET_W, words & _FORECAST_W
    if has_b and not (has_a or has_f):
        return "budget"
    if has_f and not has_b:
        return "forecast"          # pure forecast AND actual/forecast blends
    if has_a and not (has_b or has_f):
        return "actual"
    return None


_YEAR_MIN, _YEAR_MAX = 1995, 2045   # plausible reporting-timeline bounds
_MAX_SPAN_DAYS = 25 * 365           # a real pack timeline never spans decades


def _plausible_timeline(dates: dict[int, object]) -> bool:
    """Reject 'timelines' made of value cells that happen to sit in Excel's
    date-serial range: a real BS row of growing balances is MONOTONIC (beating
    the monotonic guard) but parses to absurd years — a real incident invented
    a 1979..2050 timeline from quarterly balances and hung 15 junk series on
    it. Bounds are facts about reporting packs, not heuristics about data."""
    vals = sorted(dates.values())
    if not vals:
        return False
    if vals[0].year < _YEAR_MIN or vals[-1].year > _YEAR_MAX:
        return False
    return (vals[-1] - vals[0]).days <= _MAX_SPAN_DAYS


def _timeline(cells_by_row: dict[int, list[dict]]) -> tuple[int | None, dict[int, object]]:
    """(header_row, {col: date}) — the row that is the sheet's period header:
    most parseable dates, preferring rows whose dates increase left→right (the
    same guard run._pick_timeline uses against date-serial-range data rows),
    and PLAUSIBLE as a reporting timeline (see _plausible_timeline)."""
    candidates: list[tuple[bool, int, int, dict[int, object]]] = []
    for r, rcs in cells_by_row.items():
        dates: dict[int, object] = {}
        for c in rcs:
            d = parse_any_date(effective_value(c))
            if d is not None:
                dates[c["col"]] = d
        if len(dates) >= 3 and _plausible_timeline(dates):
            vals = [dates[c] for c in sorted(dates)]
            mono = all(a <= b for a, b in zip(vals, vals[1:]))
            candidates.append((mono, len(dates), -r, dates))
    if not candidates:
        return None, {}
    mono_pool = [c for c in candidates if c[0]] or candidates
    best = max(mono_pool, key=lambda c: c[1])
    return -best[2], best[3]


def _basis_row(cells_by_row: dict[int, list[dict]], period_cols: set[int]) -> dict[int, str]:
    """{col: scenario} from the sheet's own basis row — the row where ≥3 period
    columns carry Actual/Budget/Forecast text. Deterministic fact; empty if the
    sheet has no such row."""
    best: dict[int, str] = {}
    for _r, rcs in cells_by_row.items():
        tags = {c["col"]: s for c in rcs
                if c["col"] in period_cols and (s := _scen_of(effective_value(c)))}
        if len(tags) >= 3 and len(tags) > len(best):
            best = tags
    return best


def _infer_grain(d, neighbours: list) -> str:
    """month when the column sits in a monthly cadence with its neighbours."""
    diffs = [abs((d - n).days) for n in neighbours if n is not None]
    close = min(diffs, default=None)
    if close is not None and close <= 62:
        return "month"
    if close is not None and close >= 300:
        return "year"
    return "month"


def reconcile_source_understanding(snapshot: dict, sheets: list[dict]) -> tuple[list[dict], dict]:
    """Patch the understanding in place against snapshot facts. Returns
    (patched_sheets, report). ``sheets`` entries: {sheet, periods, series}."""
    snap_by_name = {s.get("name"): s for s in snapshot.get("sheets", [])}
    report: dict[str, dict] = {}

    for sh in sheets:
        name = sh.get("sheet")
        snap = snap_by_name.get(name)
        if snap is None:
            continue
        # GEOMETRY GATE: this module's whole mechanics (date-header ROW, basis
        # ROW, series-as-rows) assume the classic orientation. Patching a
        # transposed or long-format sheet with column-oriented logic INVENTS
        # structure (it once added a 1979..2050 timeline and 15 junk series to
        # a transposed quarterly BS) — those sheets are left to the
        # orientation-aware catalogue untouched.
        from app.population.catalogue import claim_orientation
        if claim_orientation(sh.get("periods") or [], sh.get("series") or []) != "columns":
            report[name] = {"skipped": "non-classic orientation — coverage patching not applied"}
            continue
        cells = snap.get("cells", [])
        by_row: dict[int, list[dict]] = defaultdict(list)
        addr_by_rc: dict[tuple[int, int], str] = {}
        for c in cells:
            by_row[c["row"]].append(c)
            if c.get("address"):
                addr_by_rc[(c["row"], c["col"])] = c["address"]

        header_row, fact_dates = _timeline(by_row)
        claimed = sh.get("periods") or []
        claimed_cols: dict[int, dict] = {}
        for p in claimed:
            rc = a1_to_rowcol(p.get("header_cell", ""))
            if rc:
                claimed_cols[rc[1]] = p

        # 1) ADD period columns the claim missed (fact: a dated header cell).
        added_cols = 0
        for col, d in sorted(fact_dates.items()):
            if col in claimed_cols:
                continue
            addr = addr_by_rc.get((header_row, col)) or f"{column_letter(col)}{header_row}"
            neigh = [fact_dates.get(col - 1), fact_dates.get(col + 1)]
            entry = {"header_cell": addr, "date": d.isoformat(),
                     "grain": _infer_grain(d, neigh), "kind": None}
            claimed.append(entry)
            claimed_cols[col] = entry
            added_cols += 1

        # 2) SCENARIO from the basis row — the source's own declaration wins.
        basis = _basis_row(by_row, set(claimed_cols))
        tags_set = 0
        for col, scen in basis.items():
            p = claimed_cols.get(col)
            if p is not None and (p.get("kind") or None) != scen:
                p["kind"] = scen
                tags_set += 1

        # 3) ADD series the claim missed (fact: a labelled row with numbers
        #    across period columns). Meaning stays with the mapper.
        claimed_rows = {rc[0] for s in sh.get("series") or []
                        if (rc := a1_to_rowcol(s.get("label_cell", "")))}
        pcols = set(claimed_cols)
        added_series = 0
        series_capped = 0
        _MAX_ADDED_SERIES = 60   # per sheet; a huge model sheet must not balloon
        for r, rcs in sorted(by_row.items()):
            if r in claimed_rows or r == header_row:
                continue
            nums = sum(1 for c in rcs
                       if c["col"] in pcols and _is_number(effective_value(c)))
            if nums < 3:
                continue
            label = _row_label(rcs)
            if not label or _scen_of(label):    # a basis/scenario tag row is not data
                continue
            if added_series >= _MAX_ADDED_SERIES:
                series_capped += 1              # counted + reported, never silent
                continue
            label_cell = next((c for c in sorted(rcs, key=lambda c: c["col"])
                               if isinstance(effective_value(c), str)
                               and str(effective_value(c)).strip() == label), None)
            addr = (label_cell or {}).get("address") or f"A{r}"
            sh.setdefault("series", []).append({"label_cell": addr, "label": label})
            added_series += 1

        sh["periods"] = claimed
        if added_cols or tags_set or added_series or series_capped:
            dates = sorted(d for d in fact_dates.values())
            report[name] = {
                "period_cols_added": added_cols,
                "scenario_tags_set": tags_set,
                "series_added": added_series,
                "timeline": (f"{dates[0]:%Y-%m}..{dates[-1]:%Y-%m}" if dates else None),
            }
            if series_capped:
                report[name]["series_recovery_capped"] = series_capped
            logger.info("source reconciliation for %s: +%d period cols, %d scenario tags, "
                        "+%d series", name, added_cols, tags_set, added_series)
    return sheets, report
