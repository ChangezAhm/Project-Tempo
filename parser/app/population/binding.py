"""Deterministic binding: template input facts + a meaning-only mapping -> CellLinks.

This is where correctness is decided, with NO LLM in the loop:

  - which source column feeds each template period slot   (periods.pick_column)
  - one unit scale per series (raw->millions etc.)         (units.series_scale)
  - a confidence floor below which we leave the cell blank
Currencies are never converted; a declared cross-currency fill is review-noted.

The output CellLinks are consumed by the existing, tested `apply_links`, which
reads the real value from the source snapshot at the cited address. We never
re-type a value here — we only point at one.
"""

from __future__ import annotations

from collections import Counter

from app.population.catalogue import Series
from app.population.numfmt import parse_number_format
from app.population.periods import infer_grain, parse_iso_period, pick_column
from app.population.schema import CellLink, MetricMap
from app.population.units import is_count_like, reconcile_scale, resolve_scale, resolve_unit


def _col_letters(col: int) -> str:
    """1-based column index -> Excel letters (1->A, 27->AA)."""
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def _metric_key(fact: dict) -> str | None:
    return fact.get("canonical_metric") or fact.get("metric_label")


def _sum_samples(samples: list[list[float]]) -> list[float]:
    """Elementwise sum of aligned component samples (truncated to the shortest), so
    an aggregated series' magnitude/sign reconciles against the TOTAL the template
    holds rather than against one component."""
    lists = [s for s in samples if s]
    if not lists:
        return []
    n = min(len(s) for s in lists)
    return [sum(s[i] for s in lists) for i in range(n)]


def _scenario_columns(series: Series, dem_scen: str) -> list[tuple]:
    """Candidate period columns for a demanded scenario. Restrict to the demanded
    scenario ONLY when the template explicitly asks for budget/forecast; otherwise
    every column is a candidate, actuals first so a same-period tie resolves to the
    actual. as-of plays no part here — scenario is the source's own tag."""
    def scen_of(col: int) -> str:
        return series.col_scenario.get(col) or "actual"
    cols = series.period_cols
    if dem_scen in ("budget", "forecast"):
        return [pc for pc in cols if scen_of(pc[0]) == dem_scen]
    return sorted(cols, key=lambda pc: 0 if scen_of(pc[0]) == "actual" else 1)


def _dominant_sign(vals, min_n: int = 2) -> int:
    """-1 / +1 when ≥70% of the nonzero values share a sign (and there are at
    least ``min_n``), else 0 (no verdict). Mixed rows (variances) stay 0."""
    xs = [float(v) for v in (vals or [])
          if isinstance(v, (int, float)) and not isinstance(v, bool) and v]
    if len(xs) < min_n:
        return 0
    neg = sum(1 for v in xs if v < 0)
    if neg >= 0.7 * len(xs):
        return -1
    if neg <= 0.3 * len(xs):
        return 1
    return 0


def _convention_sign(text) -> int:
    """The L3 sign_convention is prose derived from the template's own formulas
    ('negative (entered as negative, added in Gross Profit formula E8+E9)').
    Leading word wins — later clauses qualify exceptions, not the convention."""
    t = (str(text or "")).strip().lower()
    if t.startswith("negative"):
        return -1
    if t.startswith("positive"):
        return 1
    return 0


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
         demand: dict, *, display_unit: str | None = None, confidence_floor: float = 0.6,
         template_context: tuple[dict, dict] | None = None,
         agg_membership: dict[str, frozenset] | None = None,
         ) -> tuple[list[CellLink], list[dict]]:
    """Returns (links, unmatched). Each template input fact becomes a CellLink with
    a fully-resolved unit_scale and sign, or an unmatched entry whose reason says
    exactly why (no mapping / low confidence / no source period / unit unresolved).
    Currencies are NOT converted — values are written as-is; when both sides
    declare a currency and they differ, the link carries a review note.

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

    # DETERMINISTIC DOUBLE-COUNT GUARD: a source amount must never be counted
    # twice INSIDE THE SAME TOTAL — that is the only way a reuse inflates a figure
    # (the Adjusted-EBITDA-adjustments case). A KPI (ARR, Headcount, Churn) shown
    # on several sheets shares no total and may repeat as often as needed.
    #
    # `agg_membership` (metric_key -> the set of totals its cells feed, from the
    # template's own formulas — aggregation.metric_totals) is the arbiter: two
    # metrics conflict over a shared source series ONLY when their totals
    # intersect. An EMPTY set means the metric feeds no total → free to repeat.
    # When it's None (no formula graph available — a parse gap), fall back to the
    # conservative GLOBAL block: any cross-metric reuse is blocked, so the
    # inflation bug can never silently return.
    #
    # Claims are resolved by priority — direct > aggregate > reconcile, then higher
    # confidence — so the strongest mapping keeps the series; a weaker metric that
    # would double-count against it (or one of its aggregated components) is blocked.
    _RANK = {"direct": 0, "aggregate": 1, "reconcile": 2}
    graph = agg_membership is not None
    claimed: dict[str, list[str]] = {}    # series_id -> metrics legitimately using it
    blocked: dict[str, str] = {}          # metric key -> the metric it would double-count against
    for m in sorted(by_metric.values(),
                    key=lambda mm: (_RANK.get(getattr(mm, "status", "direct"), 1), -mm.confidence)):
        wants = [sid for sid in ([m.series_id] + list(m.also_series_ids or []))
                 if sid and sid in catalogue]
        owner = None
        for sid in wants:
            for prior in claimed.get(sid, ()):
                shares_total = (bool(agg_membership.get(m.metric, frozenset())
                                     & agg_membership.get(prior, frozenset()))
                                if graph else True)
                if shares_total:
                    owner = prior
                    break
            if owner:
                break
        if owner is not None:
            blocked[m.metric] = owner
        else:
            for sid in wants:
                claimed.setdefault(sid, []).append(m.metric)

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
        # value_role guard: the template computes its own totals/subtotals — a
        # total row whose cells happen to be literals (not formulas) still must
        # never be WRITTEN, or we'd fight the template's own arithmetic.
        if (f.get("value_role") or "").strip().lower() in ("total", "subtotal", "header"):
            unmatched.append(_unmatched(
                f, f"template {f.get('value_role')} row — computed by the template, never written"))
            continue
        key = _metric_key(f)
        mm = by_metric.get(key)
        if mm is None:
            unmatched.append(_unmatched(f, "no source series mapped to this metric"))
            continue
        # Double-count guard: this metric's source series already feeds another
        # metric it would be summed with — writing it here too double-counts.
        if key in blocked:
            owner = blocked[key]
            reason = (f"source already feeds '{owner}' and both roll into the same template total "
                      "— not written again (avoids double counting)"
                      if agg_membership is not None else
                      f"source already used by '{owner}' — not written again (avoids double counting)")
            unmatched.append(_unmatched(f, reason))
            continue
        # A reconcile is a DELIBERATE approximation (source data cut differently) — it is
        # kept whatever its confidence, but flagged and raised for user confirmation.
        reconciled = getattr(mm, "status", "direct") == "reconcile"
        if mm.confidence < confidence_floor and not reconciled:
            unmatched.append(_unmatched(f, f"mapping confidence {mm.confidence:.2f} < floor {confidence_floor:.2f}"))
            continue
        series = catalogue.get(mm.series_id)
        if series is None:
            unmatched.append(_unmatched(f, f"mapped series '{mm.series_id}' not in catalogue"))
            continue

        # AGGREGATION: a template line that is the exact SUM of several source lines
        # (e.g. Total Revenue = NA + EMEA + APAC when the source has no single
        # total). Components must share the primary's sheet and period columns;
        # any that don't are ignored (the sum stays over aligned, cited cells).
        components = [series]
        for sid in mm.also_series_ids or []:
            s2 = catalogue.get(sid)
            if s2 is not None and s2 is not series and s2.sheet == series.sheet:
                components.append(s2)
        agg = len(components) > 1
        # magnitude/sign reconcile against the combined total, not one component.
        recon_sample = _sum_samples([c.sample for c in components]) if agg else series.sample

        # SCENARIO is demand-gated: restrict to the template's scenario only when it
        # explicitly asks for budget/forecast; otherwise take any column (actuals
        # preferred). as-of never classifies scenario.
        dem_scen = (f.get("scenario") or "").strip().lower()
        if dem_scen in ("budget", "forecast") and not agg and dem_scen in series.variants:
            # scenario-by-ROW source: the bare 'Budget'/'Forecast' row under the
            # mapped metric row carries this slot's figures — swap to that variant.
            series = series.variants[dem_scen]
            components = [series]
            recon_sample = series.sample
            cand_cols = sorted(series.period_cols, key=lambda pc: pc[0])
        elif dem_scen in ("budget", "forecast") and getattr(series, "scenario", None) == dem_scen:
            # the whole mapped ROW is tagged with the demanded scenario — every
            # dated column is a candidate.
            cand_cols = sorted(series.period_cols, key=lambda pc: pc[0])
        else:
            cand_cols = _scenario_columns(series, dem_scen)
            if dem_scen in ("budget", "forecast") and not cand_cols:
                unmatched.append(_unmatched(
                    f, f"source has no {dem_scen} column or row-variant for '{mm.series_id}'"))
                continue

        # which source column for this template period slot — align by the template
        # column's REAL date (read from its timeline) when we have it, else positional.
        sheet = f.get("sheet_name")
        tdate = dates_by_col.get((sheet, f.get("col"))) or parse_iso_period(f.get("parsed_date"))
        col = pick_column(f.get("period_index"), pc_by_sheet.get(sheet) or period_count, tdate,
                          cand_cols, grain, template_grain=sheet_grain.get(sheet),
                          point_in_time=(f.get("basis") == "point_in_time"))
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

        # COUNTS (headcount/FTEs) are dimensionless: never magnitude-rescaled —
        # the money-scale fallback once turned 512 FTEs into 0.000512. Detected
        # from either side's label (the source sheet's currency banner routinely
        # mislabels count rows as money).
        ccy_flag = None
        if is_count_like(f.get("metric_label")) or is_count_like(series.label):
            scale, sflag = 1.0, None
        else:
            # SCALE by magnitude reconciliation (labels are unreliable); flag if unverified.
            scale, sflag = resolve_scale(recon_sample, tpl_mags, series.unit, tpl_unit,
                                         fallback_scale=fallback_scale)
            if scale is None:
                unmatched.append(_unmatched(f, f"unit/scale unresolved ({sflag}); supply display_unit or check formats"))
                continue
            # No FX in the system: values are written as-is. When both sides
            # declare a currency and they differ, note it so the audit is honest.
            src_ccy, tpl_ccy = series.unit.currency, f.get("currency")
            if series.unit.kind == "money" and src_ccy and tpl_ccy and src_ccy != tpl_ccy:
                ccy_flag = f"currency_unverified:{src_ccy}->{tpl_ccy}"

        # SIGN, deterministic-first. The LLM's sign_flip is a guess from labels;
        # the template itself knows better: (1) the dominant sign of the values
        # already in the row (a prior fill is ground truth for the convention),
        # (2) the L3 sign_convention read from the template's own formulas
        # (GP = E8+E9 means costs are entered negative). LLM only as fallback.
        src_sign = _dominant_sign(recon_sample)
        tpl_sign = _dominant_sign(tpl_mags) or _convention_sign(f.get("sign_convention"))
        sign_note = None
        if src_sign and tpl_sign:
            sign_flip = src_sign != tpl_sign
            if sign_flip != mm.sign_flip:
                sign_note = "sign:template-evidence"   # we overrode the LLM's guess
        else:
            sign_flip = mm.sign_flip

        source_cell = f"{_col_letters(col)}{series.row}"
        agg_source_cells: list[str] = []
        if agg:
            # every component sits on the primary's sheet at the SAME column
            agg_source_cells = [f"{c.sheet}!{_col_letters(col)}{c.row}" for c in components[1:]]
            note = "SUM: " + " + ".join(c.label for c in components) + f" @ {series.sheet} [derived:sum]"
        else:
            note = f"{series.label} @ {series.sheet}"
        if reconciled:
            note += f" [reconciled: {(mm.assumption or 'source granularity differs')[:140]}]"
        if not any(d is not None for (_c, d, _pt) in series.period_cols):
            note += " [period:positional — source has no dates, verify alignment]"
        if sflag:
            note += f" [{sflag}]"
        if ccy_flag:   # written as-is despite differing declared currencies — review it
            note += f" [{ccy_flag}]"
        if sign_note:
            note += f" [{sign_note}]"
        links.append(CellLink(
            template_sheet=f.get("sheet_name"),
            template_cell=f.get("cell"),
            source_sheet=series.sheet,
            source_cell=source_cell,
            agg_source_cells=agg_source_cells,
            unit_scale=scale,
            sign_flip=sign_flip,
            confidence=mm.confidence,
            note=note,
        ))

    return links, unmatched
