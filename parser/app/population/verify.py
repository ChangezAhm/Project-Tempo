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

from app.population.catalogue import Series
from app.population.periods import _grain, sheet_grains
from app.population.schema import MetricMap, PlanIssue, metric_key

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
                confidence_floor: float = 0.6,
                scenario_equiv: dict[str, str] | None = None) -> list[PlanIssue]:
    """Plan-level verification (cell-level data gaps surface in execute).
    ``scenario_equiv``: user-confirmed contract substitutions ({"budget":
    "forecast"}) — a demanded scenario its equivalent can serve is not a gap."""
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
        k = metric_key(f)
        if k is None:
            continue
        sheets_of.setdefault(k, set()).add(f.get("sheet_name"))
        s = (f.get("scenario") or "").strip().lower()
        if s in ("budget", "forecast"):
            scen_of.setdefault(k, set()).add(s)

    # template sheet grains, from the template's own column dates (facts)
    _numfmt, _mags, dates_by_col = template_context or ({}, {}, {})
    sheet_grain = sheet_grains(dates_by_col)

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
        if m.confidence < confidence_floor and m.status in ("direct", "aggregate"):
            issues.append(PlanIssue(
                metric=key, code="LOW_CONFIDENCE", severity="question",
                detail=f"mapping confidence {m.confidence:.2f} < {confidence_floor:.2f}",
                suggested_resolution=f"map to '{series.label}' ({m.series_id})"))

        # SCENARIO_NO_SOURCE: the template demands budget/forecast slots this
        # series can't serve (no variant row, no tagged column) — factual check.
        # A user-confirmed equivalence counts as coverage (the executor serves
        # those slots from the equivalent scenario's columns).
        for scen in sorted(scen_of.get(key, ())):
            accepted = {scen}
            sub = (scenario_equiv or {}).get(scen)
            if sub:
                accepted.add(sub)
            has = (bool(accepted & set(series.variants or {}))
                   or series.scenario in accepted
                   or any((series.col_scenario.get(c) or "actual") in accepted
                          for (c, _d, _pt) in series.period_cols))
            if not has:
                # a data gap the planner cannot repair (it can't conjure budget
                # columns) — the executor leaves those slots blank per-fact; this
                # issue only surfaces the gap for one batched question.
                issues.append(PlanIssue(
                    metric=key, code="SCENARIO_NO_SOURCE", severity="question",
                    scenario=scen,
                    detail=f"template demands {scen} slots but the source series has no {scen} "
                           f"column or row-variant",
                    suggested_resolution=f"leave the {scen} slots blank"))

        # GRAIN_UNBRIDGEABLE: a template sheet coarser than the source series,
        # and the plan declares no rollup op — never guessed
        src_g = _finest_grain(series)
        if src_g and not m.rollup:
            for sh in sorted(sheets_of.get(key, ())):
                tgt_g = sheet_grain.get(sh)
                if tgt_g in _COARSER and _COARSER[tgt_g] > _COARSER[src_g]:
                    issues.append(PlanIssue(
                        metric=key, code="GRAIN_UNBRIDGEABLE", severity="repair",
                        detail=(f"source is {src_g}ly but template sheet '{sh}' is {tgt_g}ly and "
                                f"the plan declares no rollup (end/sum/avg)"),
                        suggested_resolution="declare the metric's rollup semantics"))
                    break

    # ANCHOR_UNFILLED (structural, no lexicons): a template TOTAL whose leaf
    # input rows are ALL unmapped while source series sit unused is a starved
    # statement anchor — the saas run left the whole revenue block empty (and GP
    # cascading wrong) while the geo-split turnover series went unused, with no
    # question asked. Severity 'repair': the planner gets one focused retry with
    # the total, its feeders, and the unused series in hand; unrepaired, it
    # becomes a question. Guarded (unused series must exist; capped) so a source
    # missing a whole statement doesn't flood the repair round.
    if agg_membership:
        floor_held = {mk for mk, m in by_metric.items()
                      if m.series_id and m.confidence < confidence_floor
                      and m.status in ("direct", "aggregate")}
        used_ids = {sid for m in by_metric.values() if m.series_id
                    for sid in ([m.series_id] + list(m.also_series_ids or []))}
        unused = [s.label for sid, s in catalogue.items() if sid not in used_ids]
        feeders_of: dict = {}
        for mk, totals in agg_membership.items():
            for t in totals:
                feeders_of.setdefault(t, set()).add(mk)
        starved = []
        for total, feeders in feeders_of.items():
            in_demand = [mk for mk in feeders if mk in demanded]
            # a feeder counts as effectively-unfilled when it has no series OR
            # its fill would be held at the confidence floor
            if in_demand and all((not (by_metric.get(mk) and by_metric[mk].series_id))
                                 or mk in floor_held for mk in in_demand):
                starved.append((total, in_demand))
        starved.sort(key=lambda tf: -len(tf[1]))
        for total, feeder_keys in starved[:6]:
            # RESOLUTION LADDER, tier 2: when the starved anchor's ONLY hope is a
            # mapped-but-below-floor fill, holding it means an empty statement —
            # fill it as a FLAGGED RECONCILE instead (visible, filed as a
            # question, user-reversible). The floor stays intact everywhere else.
            rescued = [mk for mk in feeder_keys if mk in floor_held]
            for mk in rescued:
                m = by_metric[mk]
                m.status = "reconcile"
                if not m.assumption:
                    m.assumption = (f"anchor reconstruction at confidence {m.confidence:.2f} — "
                                    "filled to prevent an empty statement block; confirm or remap")
                issues.append(PlanIssue(
                    metric=mk, code="ANCHOR_UNFILLED", severity="default",
                    detail=(f"'{mk}' is the only mapped feeder of template total "
                            f"{total[0]}!r{total[1]} and sat below the confidence floor"),
                    resolution="filled as flagged reconcile instead of held (empty anchor beats floor)"))
            if rescued:
                continue
            if not unused:
                continue
            detail = (f"template total {total[0]}!r{total[1]} sums "
                      f"{', '.join(map(str, feeder_keys[:6]))} — ALL unmapped, so the "
                      f"total computes empty/zero and everything downstream is wrong. "
                      f"UNUSED source series: {', '.join(unused[:8])}")
            for mk in feeder_keys:
                issues.append(PlanIssue(
                    metric=mk, code="ANCHOR_UNFILLED", severity="repair",
                    detail=detail,
                    suggested_resolution=("reconcile the corresponding source total onto the "
                                          "dominant component, or aggregate the unused component "
                                          "series, or needs_decision — never all-unavailable")))
        # rescued metrics must not ALSO carry a LOW_CONFIDENCE hold
        rescued_all = {i.metric for i in issues
                       if i.code == "ANCHOR_UNFILLED" and i.severity == "default"}
        issues = [i for i in issues
                  if not (i.code == "LOW_CONFIDENCE" and i.metric in rescued_all)]

    # DOUBLE_COUNT: same claim/priority logic as the legacy binder, expressed as
    # typed issues. Two metrics conflict over a shared series only when their
    # template totals intersect (formula-graph scoped); no graph -> global block.
    _RANK = {"direct": 0, "aggregate": 1, "reconcile": 2}
    graph = agg_membership is not None
    member = agg_membership or {}
    claimed: dict[str, list[str]] = {}
    for m in sorted(by_metric.values(),
                    key=lambda mm: (_RANK.get(mm.status, 1), -mm.confidence)):
        wants = [sid for sid in ([m.series_id] + list(m.also_series_ids or []))
                 if sid and sid in catalogue]
        owner = None
        for sid in wants:
            for prior in claimed.get(sid, ()):
                shares = (bool(member.get(m.metric, frozenset())
                               & member.get(prior, frozenset())) if graph else True)
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
