"""Deterministic binding: template input facts + a meaning-only mapping -> CellLinks.

This is where correctness is decided, with NO LLM in the loop:

  - which source column feeds each template period slot   (periods.pick_column)
  - one unit scale per series (raw->millions etc.)         (units.series_scale)
  - currency, folded into the scale or blocked if unknown  (fx.multiplier)
  - a confidence floor below which we leave the cell blank

The output CellLinks are consumed by the existing, tested `apply_links`, which
reads the real value from the source snapshot at the cited address. We never
re-type a value here — we only point at one.
"""

from __future__ import annotations

from collections import Counter

from app.population import fx
from app.population.catalogue import Series
from app.population.numfmt import parse_number_format
from app.population.periods import infer_grain, parse_iso_period, pick_column
from app.population.schema import CellLink, MetricMap
from app.population.units import reconcile_scale, resolve_scale, resolve_unit


def _col_letters(col: int) -> str:
    """1-based column index -> Excel letters (1->A, 27->AA)."""
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def _metric_key(fact: dict) -> str | None:
    return fact.get("canonical_metric") or fact.get("metric_label")


def _unmatched(fact: dict, reason: str) -> dict:
    return {
        "template_sheet": fact.get("sheet_name"),
        "template_cell": fact.get("cell"),
        "metric": _metric_key(fact),
        "period_index": fact.get("period_index"),
        "scenario": fact.get("scenario"),
        "reason": reason,
    }


def bind(facts: list[dict], catalogue: dict[str, Series], metric_maps: list[MetricMap],
         demand: dict, *, target_currency: str | None = None, fx_rate: float | None = None,
         display_unit: str | None = None, confidence_floor: float = 0.6,
         template_context: tuple[dict, dict] | None = None,
         ) -> tuple[list[CellLink], list[dict]]:
    """Returns (links, unmatched). Each template input fact becomes a CellLink with
    a fully-resolved unit_scale (scale*FX) and sign, or an unmatched entry whose
    reason says exactly why (no mapping / low confidence / no source period /
    unit unresolved / currency mismatch). Blank-and-explain beats wrong.

    template_context = (numfmt_by_cell, magnitudes_by_row, dates_by_col) read from the
    template snapshot: lets scale be decided by MAGNITUDE reconciliation (robust) and
    periods by ACTUAL DATE (template column date ↔ source column date). A link whose
    scale couldn't be magnitude-verified keeps a 'scale_unverified' tag in its note."""
    tc = template_context or ({}, {}, {})
    numfmt_by_cell = tc[0] if len(tc) > 0 else {}
    mags_by_row = tc[1] if len(tc) > 1 else {}
    dates_by_col = tc[2] if len(tc) > 2 else {}
    # template timeline grain per sheet, inferred from the spacing of its column dates
    _sheet_dates: dict[str, list] = {}
    for (sh, _col), d in dates_by_col.items():
        _sheet_dates.setdefault(sh, []).append(d)
    sheet_grain = {sh: infer_grain(ds) for sh, ds in _sheet_dates.items()}
    by_metric: dict[str, MetricMap] = {}
    for m in metric_maps:
        if m.series_id:
            # keep the highest-confidence mapping if a metric appears twice
            cur = by_metric.get(m.metric)
            if cur is None or m.confidence > cur.confidence:
                by_metric[m.metric] = m

    period_count = int(demand.get("period_count") or 0)
    pc_by_sheet: dict = demand.get("period_count_by_sheet") or {}
    grain = demand.get("period_grain") or "month"

    # Default scale for rows that have NO template anchor: the ×10^(3n) that the
    # rows which DO have an anchor most commonly reconcile to (e.g. raw->millions =
    # 1e-6). Keeps unanchored rows in line with the rest of the template instead of
    # a blind ×1. (Best-effort; unanchored fills are flagged for review regardless.)
    votes: Counter = Counter()
    for f in facts:
        mm = by_metric.get(_metric_key(f))
        s = catalogue.get(mm.series_id) if mm else None
        if s is None:
            continue
        sc = reconcile_scale(s.sample, mags_by_row.get((f.get("sheet_name"), f.get("row")), []))
        if sc is not None:
            votes[sc] += 1
    fallback_scale = votes.most_common(1)[0][0] if votes else 1.0

    links: list[CellLink] = []
    unmatched: list[dict] = []

    for f in facts:
        key = _metric_key(f)
        mm = by_metric.get(key)
        if mm is None:
            unmatched.append(_unmatched(f, "no source series mapped to this metric"))
            continue
        if mm.confidence < confidence_floor:
            unmatched.append(_unmatched(f, f"mapping confidence {mm.confidence:.2f} < floor {confidence_floor:.2f}"))
            continue
        series = catalogue.get(mm.series_id)
        if series is None:
            unmatched.append(_unmatched(f, f"mapped series '{mm.series_id}' not in catalogue"))
            continue

        # v1: only fill actuals from an actuals source; flag forecast/budget slots.
        scen = (f.get("scenario") or "").strip().lower()
        if scen not in ("", "actual", "actuals", "unknown"):
            unmatched.append(_unmatched(f, f"scenario '{scen}' not available from source (actuals only)"))
            continue

        # which source column for this template period slot — align by the template
        # column's REAL date (read from its timeline) when we have it, else positional.
        sheet = f.get("sheet_name")
        tdate = dates_by_col.get((sheet, f.get("col"))) or parse_iso_period(f.get("parsed_date"))
        col = pick_column(f.get("period_index"), pc_by_sheet.get(sheet) or period_count, tdate,
                          series.period_cols, grain, template_grain=sheet_grain.get(sheet))
        if col is None:
            unmatched.append(_unmatched(f, f"no source column for period_index={f.get('period_index')} ({grain})"))
            continue

        # the template cell's own signals: number format (kind/currency) + the
        # magnitudes already present in its row (the truth about what it holds).
        sheet, cell = f.get("sheet_name"), (f.get("cell") or "").upper()
        tpl_fmt = numfmt_by_cell.get((sheet, cell))
        tpl_unit = resolve_unit(f.get("unit"))
        if tpl_unit.kind == "unknown":
            tpl_unit = parse_number_format(tpl_fmt)
        if tpl_unit.kind == "unknown" and display_unit:
            tpl_unit = resolve_unit(display_unit)
        tpl_mags = mags_by_row.get((sheet, f.get("row")), [])

        # SCALE by magnitude reconciliation (labels are unreliable); flag if unverified.
        scale, sflag = resolve_scale(series.sample, tpl_mags, series.unit, tpl_unit,
                                     fallback_scale=fallback_scale)
        if scale is None:
            unmatched.append(_unmatched(f, f"unit/scale unresolved ({sflag}); supply display_unit or check formats"))
            continue

        # currency, folded into the scale
        tgt_ccy = f.get("currency") or target_currency
        fx_mult, fxflag = fx.multiplier(series.unit.currency, tgt_ccy, fx_rate)
        if fx_mult is None:
            unmatched.append(_unmatched(f, fxflag))
            continue

        source_cell = f"{_col_letters(col)}{series.row}"
        note = f"{series.label} @ {series.sheet}"
        if sflag:
            note += f" [{sflag}]"
        if fxflag:   # filled, but one side's currency was undetectable — review it
            note += f" [{fxflag}]"
        links.append(CellLink(
            template_sheet=f.get("sheet_name"),
            template_cell=f.get("cell"),
            source_sheet=series.sheet,
            source_cell=source_cell,
            unit_scale=scale * fx_mult,
            sign_flip=mm.sign_flip,
            confidence=mm.confidence,
            note=note,
        ))

    return links, unmatched
