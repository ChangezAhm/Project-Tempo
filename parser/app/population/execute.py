"""Fill-Plan executor (docs/Fill-Plan-Architecture.md §2.5).

Expands verified SeriesFill entries across the template's input slots and builds
CellLinks — pure mechanics over facts. Semantics (which series, rollup op, units,
sign, scenario) come from the PLAN; this module computes, cross-checks against
template evidence, and cites. Where evidence contradicts the plan it applies the
resolution ladder: strong evidence -> flagged auto-resolution (never silent);
no evidence either way -> a typed issue for the question tier.

What was deliberately DELETED relative to the legacy binder (rule freeze):
the rollup heuristic cascade (basis-override, percent->avg, count->end), the
count-label scale bypass, and the modal fallback scale. Their jobs belong to
the planner (intent) or to SCALE_CONFLICT/SIGN_CONFLICT evidence handling.
"""

from __future__ import annotations

from app.population.binding import (
    _col_letters, _convention_sign, _dominant_sign, _metric_key, _scenario_columns,
    _sum_samples, _unmatched,
)
from app.population.catalogue import Series
from app.population.numfmt import parse_number_format
from app.population.periods import align_slot, infer_grain, parse_iso_period
from app.population.schema import CellLink, MetricMap, PlanIssue
from app.population.units import reconcile_scale, resolve_unit

# align_slot failure code -> (issue code or None, severity, suggested resolution)
_ALIGN_ISSUES = {
    "bucket_incomplete": ("BUCKET_INCOMPLETE", "question",
                          "leave blank (a partial bucket is not a quarter/year); "
                          "answer 'sum the partial months' to override"),
    "period_end_missing": ("PERIOD_END_MISSING", "question",
                           "leave blank; answer with the month to use instead"),
    "grain_unbridgeable": ("GRAIN_UNBRIDGEABLE", "question",
                           "declare the metric's rollup semantics (end/sum/avg)"),
    # honest data gaps — reported as reasons, not questions (nothing to decide)
    "no_column_in_bucket": (None, "", None),
    "no_source_periods": (None, "", None),
    "positional_out_of_range": (None, "", None),
    "no_slot_index": (None, "", None),
}


def _target_unit(fill: MetricMap, f: dict, numfmt_by_cell: dict, display_unit: str | None):
    """The template side's unit: the plan's reading first, then the fact's own
    unit tag, the cell number format, and the run-level display_unit — all facts."""
    if fill.target_unit:
        u = resolve_unit(fill.target_unit)
        if u.kind != "unknown":
            return u
    u = resolve_unit(f.get("unit"))
    if u.kind == "unknown":
        u = parse_number_format(numfmt_by_cell.get((f.get("sheet_name"), (f.get("cell") or "").upper())))
    if u.kind == "unknown" and display_unit:
        u = resolve_unit(display_unit)
    return u


def _resolve_scale(fill: MetricMap, series: Series, recon_sample, tpl_mags, tgt_u):
    """(scale, flag, issue_code). Declared units give the scale; template
    magnitudes are cross-checking EVIDENCE. Disagreement resolves to the evidence
    WITH a visible flag; no verdict from either side is a SCALE_CONFLICT."""
    src_u = resolve_unit(fill.source_unit) if fill.source_unit else series.unit
    kinds = {src_u.kind, tgt_u.kind}
    if kinds & {"percent", "ratio"}:
        if src_u.kind == tgt_u.kind:
            return 1.0, None, None
        if "money" in kinds:
            return None, "unit_kind_mismatch", "UNIT_KIND_MISMATCH"
        return 1.0, None, None            # percent vs unknown: values pass as-is
    if "money" not in kinds:
        return 1.0, None, None            # counts/ratios both sides: dimensionless

    declared = None
    if src_u.kind == "money" and tgt_u.kind == "money":
        declared = (src_u.base or 1.0) / (tgt_u.base or 1.0)
    mag = reconcile_scale(recon_sample, tpl_mags)

    if declared is not None and mag is None:
        flag = None if tpl_mags else "scale_unverified:no_template_magnitude"
        if tpl_mags:  # magnitudes exist but wouldn't reconcile cleanly — say so
            flag = "scale_unverified:magnitude_mismatch"
        return declared, (flag if declared != 1.0 or tpl_mags else None), None
    if declared is None and mag is not None:
        return mag, "scale:magnitude-only", None
    if declared is not None and mag is not None:
        ratio = declared / mag if mag else 0
        if 1 / 3 <= ratio <= 3:
            return declared, None, None   # they agree
        # conflict: the template's own numbers are the stronger evidence — use
        # them, flagged, and surface the conflict
        return mag, f"scale:auto-resolved to template magnitude (plan implied x{declared:g})", "SCALE_CONFLICT"
    return None, "scale_unknown", "SCALE_CONFLICT"


def execute_plan(facts: list[dict], catalogue: dict[str, Series], fills: list[MetricMap],
                 demand: dict, *, display_unit: str | None = None,
                 template_context: tuple[dict, dict, dict] | None = None,
                 blocked: dict[str, str] | None = None,
                 ) -> tuple[list[CellLink], list[dict], list[PlanIssue]]:
    """Returns (links, unmatched, issues). ``blocked`` maps metrics with
    unresolved blocking issues to a reason — their cells stay blank, explained."""
    tc = template_context or ({}, {}, {})
    numfmt_by_cell, mags_by_row, dates_by_col = (tc + ({}, {}, {}))[:3]
    blocked = blocked or {}

    sheet_dates: dict[str, list] = {}
    for (sh, _c), d in dates_by_col.items():
        sheet_dates.setdefault(sh, []).append(d)
    sheet_grain = {sh: infer_grain(ds) for sh, ds in sheet_dates.items()}

    # one entry per metric (or per metric+scenario when the plan splits them)
    by_metric: dict[str, list[MetricMap]] = {}
    for m in fills:
        by_metric.setdefault(m.metric, []).append(m)

    def fill_for(key: str, scen: str):
        cands = by_metric.get(key) or []
        exact = [m for m in cands if (m.scenario or "").lower() == scen and m.series_id]
        anys = [m for m in cands if not m.scenario and m.series_id]
        pool = exact or anys or [m for m in cands if m.series_id]
        return max(pool, key=lambda m: m.confidence) if pool else (cands[0] if cands else None)

    period_count = int(demand.get("period_count") or 0)
    pc_by_sheet: dict = demand.get("period_count_by_sheet") or {}
    grain = demand.get("period_grain") or "month"

    links: list[CellLink] = []
    unmatched: list[dict] = []
    issue_by: dict[tuple[str, str], PlanIssue] = {}

    def note_issue(metric: str, code: str, severity: str, detail: str,
                   suggested: str | None, cell: str | None, resolution: str | None = None):
        key = (metric, code)
        it = issue_by.get(key)
        if it is None:
            it = PlanIssue(metric=metric, code=code, severity=severity, detail=detail,
                           suggested_resolution=suggested, resolution=resolution)
            issue_by[key] = it
        if cell and len(it.cells) < 40:
            it.cells.append(cell)

    for f in facts:
        # CONSTRAINT: the template computes its own totals — never written.
        if (f.get("value_role") or "").strip().lower() in ("total", "subtotal", "header"):
            unmatched.append(_unmatched(
                f, f"template {f.get('value_role')} row — computed by the template, never written"))
            continue
        key = _metric_key(f)
        cell_ref = f"{f.get('sheet_name')}!{f.get('cell')}"
        dem_scen = (f.get("scenario") or "").strip().lower()
        fill = fill_for(key, dem_scen)
        if fill is None or not fill.series_id:
            reason = "no source series mapped to this metric"
            if fill is not None and fill.note:
                reason += f" — {fill.note[:140]}"
            unmatched.append(_unmatched(f, reason))
            continue
        if key in blocked:
            unmatched.append(_unmatched(f, f"held for review — {blocked[key]}"))
            continue
        series = catalogue.get(fill.series_id)
        if series is None:      # verified upstream; belt only
            unmatched.append(_unmatched(f, f"mapped series '{fill.series_id}' not in catalogue"))
            continue

        # aggregate components (same sheet — verified upstream)
        components = [series]
        for sid in fill.also_series_ids or []:
            s2 = catalogue.get(sid)
            if s2 is not None and s2 is not series and s2.sheet == series.sheet:
                components.append(s2)
        agg = len(components) > 1
        recon_sample = _sum_samples([c.sample for c in components]) if agg else series.sample
        reconciled = getattr(fill, "status", "direct") == "reconcile"

        # SCENARIO: factual column/variant selection (tags from the source's own
        # understanding); the demanded scenario comes from the template fact.
        if dem_scen in ("budget", "forecast") and not agg and dem_scen in (series.variants or {}):
            series = series.variants[dem_scen]
            components = [series]
            recon_sample = series.sample
            cand_cols = sorted(series.period_cols, key=lambda pc: pc[0])
        elif dem_scen in ("budget", "forecast") and getattr(series, "scenario", None) == dem_scen:
            cand_cols = sorted(series.period_cols, key=lambda pc: pc[0])
        else:
            cand_cols = _scenario_columns(series, dem_scen)
            if dem_scen in ("budget", "forecast") and not cand_cols:
                unmatched.append(_unmatched(
                    f, f"source has no {dem_scen} column or row-variant for '{fill.series_id}'"))
                continue

        # PERIOD: mechanical alignment under the plan's declared semantics
        sheet = f.get("sheet_name")
        tdate = dates_by_col.get((sheet, f.get("col"))) or parse_iso_period(f.get("parsed_date"))
        picked, why = align_slot(f.get("period_index"), pc_by_sheet.get(sheet) or period_count,
                                 tdate, cand_cols, grain, template_grain=sheet_grain.get(sheet),
                                 rollup=fill.rollup)
        if picked is None:
            unmatched.append(_unmatched(f, f"no source column for this period ({why})"))
            code, sev, sugg = _ALIGN_ISSUES.get(why, (None, "", None))
            if code:
                note_issue(key, code, sev, f"{why} for '{key}'", sugg, cell_ref)
            continue
        cols, period_op = picked
        if period_op == "avg" and agg:
            unmatched.append(_unmatched(
                f, "avg rollup over an aggregated multi-series line — ambiguous, not filled"))
            continue
        col = cols[0]

        # SCALE: declared units compute it; template magnitudes cross-check it.
        tpl_mags = mags_by_row.get((sheet, f.get("row")), [])
        tgt_u = _target_unit(fill, f, numfmt_by_cell, display_unit)
        scale, sflag, sc_code = _resolve_scale(fill, series, recon_sample, tpl_mags, tgt_u)
        if scale is None:
            unmatched.append(_unmatched(
                f, f"unit/scale unresolved ({sflag}) — plan declared "
                   f"source_unit={fill.source_unit!r} target_unit={fill.target_unit!r}"))
            if sc_code:
                note_issue(key, sc_code, "question" if sc_code == "SCALE_CONFLICT" else "block",
                           f"units for '{key}' could not be determined or are incompatible",
                           "state the source unit (e.g. \"USD'000\") in your answer", cell_ref)
            continue
        if sc_code == "SCALE_CONFLICT":   # auto-resolved to evidence, visibly
            note_issue(key, sc_code, "default",
                       f"declared units disagreed with the template's magnitudes for '{key}'",
                       None, cell_ref, resolution=f"used template-magnitude scale x{scale:g}")

        # SIGN: the plan's call, cross-checked against template evidence.
        src_sign = _dominant_sign(recon_sample)
        tpl_sign = _dominant_sign(tpl_mags) or _convention_sign(f.get("sign_convention"))
        sign_flip, sign_note = fill.sign_flip, None
        if src_sign and tpl_sign:
            evid_flip = src_sign != tpl_sign
            if evid_flip != fill.sign_flip:
                sign_flip = evid_flip
                sign_note = "sign:auto-resolved:template-evidence"
                note_issue(key, "SIGN_CONFLICT", "default",
                           f"plan sign for '{key}' ({fill.sign_basis or 'unstated'}) contradicts "
                           f"the template's own values", None, cell_ref,
                           resolution="used the sign the template's existing values imply")

        # currency: never converted; differing declarations are review-noted
        src_ccy = (resolve_unit(fill.source_unit).currency if fill.source_unit else None) \
            or series.unit.currency
        tpl_ccy = f.get("currency")
        ccy_flag = None
        if src_ccy and tpl_ccy and src_ccy != tpl_ccy and tgt_u.kind == "money":
            ccy_flag = f"currency_unverified:{src_ccy}->{tpl_ccy}"

        source_cell = f"{_col_letters(col)}{series.row}"
        agg_source_cells = [f"{series.sheet}!{_col_letters(c)}{series.row}" for c in cols[1:]]
        if agg:
            agg_source_cells += [f"{c.sheet}!{_col_letters(cc)}{c.row}"
                                 for c in components[1:] for cc in cols]
            note = "SUM: " + " + ".join(c.label for c in components) + f" @ {series.sheet} [derived:sum]"
        else:
            note = f"{series.label} @ {series.sheet}"
        if len(cols) > 1:
            note += f" [derived:{period_op} of {len(cols)} monthly columns]"
        if reconciled:
            note += f" [reconciled: {(fill.assumption or 'source granularity differs')[:140]}]"
        if not any(d is not None for (_c, d, _pt) in series.period_cols):
            note += " [period:positional — source has no dates, verify alignment]"
        if sflag:
            note += f" [{sflag}]"
        if ccy_flag:
            note += f" [{ccy_flag}]"
        if sign_note:
            note += f" [{sign_note}]"
        links.append(CellLink(
            template_sheet=sheet, template_cell=f.get("cell"),
            source_sheet=series.sheet, source_cell=source_cell,
            agg_source_cells=agg_source_cells,
            agg_op="avg" if period_op == "avg" else "sum",
            unit_scale=scale, sign_flip=sign_flip,
            confidence=fill.confidence, note=note,
        ))

    return links, unmatched, list(issue_by.values())
