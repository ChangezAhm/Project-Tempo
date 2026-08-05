"""Fill-Plan verifier — the deterministic type-checker (docs/Fill-Plan-Architecture.md §2.3).

Checks the planner's SeriesFill entries against workbook FACTS and CONSTRAINTS
and returns typed PlanIssues. It never re-decides semantics: rollup, units,
sign and scenario are the plan's; this module only says whether the plan can be
executed against reality, and exactly why not.

Severity drives the resolution ladder (§2.4):
  repair    — the planner gets one shot at revising the entry
  default   — a high-confidence default is applied AND flagged (never silent)
  question  — batched into a one-tap review item with a suggested answer
  block     — a hard constraint (protected cell, error value); stays blank
"""

from __future__ import annotations

from app.population.binding import _metric_key
from app.population.catalogue import Series
from app.population.periods import _grain, infer_grain
from app.population.schema import MetricMap, PlanIssue

# Issue codes that block a metric's cells from being written until resolved.
# SCENARIO_NO_SOURCE is deliberately NOT here: it is a per-slot data gap (the
# budget columns stay blank, factually, in execute) — blocking the whole metric
# once held 91 actual cells hostage to missing budget data.
BLOCKING = {"SERIES_NOT_FOUND", "COMPONENT_MISMATCH", "DOUBLE_COUNT",
            "LOW_CONFIDENCE", "GRAIN_UNBRIDGEABLE"}


def _finest_grain(series: Series) -> str | None:
    grains = {_grain(pt) for (_c, d, pt) in series.period_cols if d is not None}
    for g in ("month", "quarter", "year"):
        if g in grains:
            return g
    return None


_COARSER = {"month": 0, "quarter": 1, "year": 2}


def verify_plan(fills: list[MetricMap], catalogue: dict[str, Series], facts: list[dict],
                demand: dict, template_context: tuple[dict, dict, dict],
                agg_membership: dict[str, frozenset] | None,
                confidence_floor: float = 0.6) -> list[PlanIssue]:
    """Plan-level verification (cell-level data gaps surface in execute)."""
    issues: list[PlanIssue] = []
    by_metric: dict[str, MetricMap] = {}
    for m in fills:
        cur = by_metric.get(m.metric)
        if cur is None or m.confidence > cur.confidence:
            by_metric[m.metric] = m

    # facts grouped per metric: which sheets/scenarios the template demands
    sheets_of: dict[str, set] = {}
    scen_of: dict[str, set] = {}
    for f in facts:
        k = _metric_key(f)
        if k is None:
            continue
        sheets_of.setdefault(k, set()).add(f.get("sheet_name"))
        s = (f.get("scenario") or "").strip().lower()
        if s in ("budget", "forecast"):
            scen_of.setdefault(k, set()).add(s)

    # template sheet grains, from the template's own column dates (facts)
    dates_by_col = template_context[2] if len(template_context) > 2 else {}
    sheet_dates: dict[str, list] = {}
    for (sh, _c), d in dates_by_col.items():
        sheet_dates.setdefault(sh, []).append(d)
    sheet_grain = {sh: infer_grain(ds) for sh, ds in sheet_dates.items()}

    # PLAN_INCOMPLETE: a demanded metric with no plan entry at all
    demanded = {m.get("metric") for m in (demand.get("metrics") or [])}
    for key in sorted(k for k in demanded if k and k not in by_metric):
        issues.append(PlanIssue(metric=key, code="PLAN_INCOMPLETE", severity="repair",
                                detail="no plan entry for this demanded metric"))

    for key, m in by_metric.items():
        if not m.series_id:      # needs_decision / unavailable — already an ask/blank
            continue
        series = catalogue.get(m.series_id)
        if series is None:
            issues.append(PlanIssue(metric=key, code="SERIES_NOT_FOUND", severity="repair",
                                    detail=f"cited series '{m.series_id}' is not in the catalogue"))
            continue
        for sid in m.also_series_ids or []:
            comp = catalogue.get(sid)
            if comp is None:
                issues.append(PlanIssue(metric=key, code="COMPONENT_MISMATCH", severity="repair",
                                        detail=f"aggregate component '{sid}' is not in the catalogue"))
            elif comp.sheet != series.sheet:
                issues.append(PlanIssue(
                    metric=key, code="COMPONENT_MISMATCH", severity="repair",
                    detail=(f"component '{sid}' is on sheet '{comp.sheet}' but the primary is on "
                            f"'{series.sheet}' — a cross-sheet sum can't share period columns")))

        # LOW_CONFIDENCE: not a silent blank — a question carrying the plan's own proposal
        if m.confidence < confidence_floor and getattr(m, "status", "direct") in ("direct", "aggregate"):
            issues.append(PlanIssue(
                metric=key, code="LOW_CONFIDENCE", severity="question",
                detail=f"mapping confidence {m.confidence:.2f} < {confidence_floor:.2f}",
                suggested_resolution=f"map to '{series.label}' ({m.series_id})"))

        # SCENARIO_NO_SOURCE: the template demands budget/forecast slots this
        # series can't serve (no variant row, no tagged column) — factual check
        for scen in sorted(scen_of.get(key, ())):
            has = (scen in (series.variants or {})
                   or getattr(series, "scenario", None) == scen
                   or any((series.col_scenario.get(c) or "actual") == scen
                          for (c, _d, _pt) in series.period_cols))
            if not has:
                # a data gap the planner cannot repair (it can't conjure budget
                # columns) — the executor leaves those slots blank per-fact; this
                # issue only surfaces the gap for one batched question.
                issues.append(PlanIssue(
                    metric=key, code="SCENARIO_NO_SOURCE", severity="question",
                    detail=f"template demands {scen} slots but the source series has no {scen} "
                           f"column or row-variant",
                    suggested_resolution=f"leave the {scen} slots blank"))

        # GRAIN_UNBRIDGEABLE: a template sheet coarser than the source series,
        # and the plan declares no rollup op — never guessed
        src_g = _finest_grain(series)
        if src_g and not m.rollup:
            for sh in sorted(sheets_of.get(key, ())):
                tgt_g = sheet_grain.get(sh)
                if tgt_g in _COARSER and _COARSER.get(tgt_g, 0) > _COARSER.get(src_g, 0):
                    issues.append(PlanIssue(
                        metric=key, code="GRAIN_UNBRIDGEABLE", severity="repair",
                        detail=(f"source is {src_g}ly but template sheet '{sh}' is {tgt_g}ly and "
                                f"the plan declares no rollup (end/sum/avg)"),
                        suggested_resolution="declare the metric's rollup semantics"))
                    break

    # DOUBLE_COUNT: same claim/priority logic as the legacy binder, expressed as
    # typed issues. Two metrics conflict over a shared series only when their
    # template totals intersect (formula-graph scoped); no graph -> global block.
    _RANK = {"direct": 0, "aggregate": 1, "reconcile": 2}
    graph = agg_membership is not None
    claimed: dict[str, list[str]] = {}
    for m in sorted(by_metric.values(),
                    key=lambda mm: (_RANK.get(getattr(mm, "status", "direct"), 1), -mm.confidence)):
        wants = [sid for sid in ([m.series_id] + list(m.also_series_ids or []))
                 if sid and sid in catalogue]
        owner = None
        for sid in wants:
            for prior in claimed.get(sid, ()):
                shares = (bool((agg_membership or {}).get(m.metric, frozenset())
                               & (agg_membership or {}).get(prior, frozenset())) if graph else True)
                if shares:
                    owner = prior
                    break
            if owner:
                break
        if owner is not None:
            issues.append(PlanIssue(
                metric=m.metric, code="DOUBLE_COUNT", severity="repair",
                detail=(f"source series already feeds '{owner}'"
                        + (" and both roll into the same template total" if graph else "")
                        + " — writing it twice would double-count"),
                suggested_resolution=f"keep it on '{owner}'; map this metric elsewhere or mark unavailable"))
        else:
            for sid in wants:
                claimed.setdefault(sid, []).append(m.metric)

    return issues


def blocked_metrics(issues: list[PlanIssue]) -> dict[str, str]:
    """metric -> human reason, for every metric with an unresolved blocking issue."""
    out: dict[str, str] = {}
    for i in issues:
        if i.code in BLOCKING and i.resolution is None:
            out.setdefault(i.metric, f"{i.code}: {i.detail}")
    return out
