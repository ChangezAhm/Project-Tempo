"""The fill plan: mapping (one-pass / batched / cached), the deterministic
guards over it (rescue, conservation, aggregation scope), the contract overlay,
and the verify → execute core with its two bounded model loops (outcome
revision, tie-out). Three copies of the verify→execute block in the monolith
collapse into ``verify_and_execute`` here — the guards can never diverge again."""

from __future__ import annotations

import logging
from collections import Counter

from app.population import progress, source_cache
from app.population.apply import apply_links
from app.population.mapping import map_metrics
from app.population.pipeline.state import RunState, best_effort
from app.population.schema import metric_key

logger = logging.getLogger(__name__)


def verify_and_execute(state: RunState, issues: list | None = None) -> None:
    """The one place a plan becomes links: verify (unless the caller already
    verified) → blocked set → execute → ``plan_issues``. Every path — initial,
    outcome revision, tie-out revision — runs THIS, so nothing bypasses the
    guards."""
    from app.population.execute import execute_plan
    from app.population.verify import blocked_metrics, verify_plan
    if issues is None:
        issues = verify_plan(state.metric_maps, state.catalogue, state.target_inputs,
                             state.demand, state.template_context, state.agg_membership,
                             scenario_equiv=state.scen_equiv)
    state.blocked = blocked_metrics(issues)
    state.links, state.bind_unmatched, exec_issues = execute_plan(
        state.target_inputs, state.catalogue, state.metric_maps, state.demand,
        display_unit=state.display_unit, template_context=state.template_context,
        blocked=state.blocked, scenario_equiv=state.scen_equiv)
    state.plan_issues = issues + exec_issues


# ---- stages ------------------------------------------------------------------

def stage_mapping(state: RunState) -> None:
    progress.set_stage(state.target_template_id, "planning",
                       f"{len(state.demand['metrics'])} metrics")
    if state.onepass_maps is not None:
        state.metric_maps, mapping_failed = list(state.onepass_maps), []
    else:
        state.metric_maps, mapping_failed = map_metrics(
            state.demand["metrics"], state.catalogue,
            context=state.biz_context, grids=state.grids_block)
    # ABORT INSURANCE: persist the freshly-mapped plan immediately — if any
    # later stage hits the spend cap, the retry resumes from here for ~$0.
    if state.plan_key and not state.plan_cached and state.metric_maps:
        try:
            source_cache.put(state.plan_key, {"stage": "mapped",
                                              "maps": [m.model_dump(mode="json")
                                                       for m in state.metric_maps]})
        except Exception:  # noqa: BLE001 — cache is best-effort
            pass
    if mapping_failed:
        # LOUD: name the affected metrics and file a durable review item —
        # a silently-dropped batch once meant up to 80 metrics vanished.
        state.routing["mapping_failed_metrics"] = mapping_failed[:80]
        try:
            from app.review.items import make_item
            state.ask([make_item(
                source="populate", kind="judgment",
                question=(f"{len(mapping_failed)} template metric(s) went unmapped because a "
                          "mapping batch failed after retry — re-run populate for these."),
                why=f"Affected: {', '.join(mapping_failed[:15])}"
                    + ("…" if len(mapping_failed) > 15 else ""),
            )])
        except Exception as e:  # noqa: BLE001
            logger.warning("could not file mapping-failure review item: %s", e)


def stage_rescue(state: RunState) -> None:
    """DEEP RESCUE (digest mode only): give each metric the fast batched pass
    could NOT place its own focused agent (one metric + the whole catalogue),
    run in parallel. In grid mode the OUTCOME-driven revision loop (full
    context) supersedes deep rescue (per-metric Sonnet agents WITHOUT grids —
    now strictly weaker than the pass they'd be rescuing)."""
    if not (state.deep_rescue and state.catalogue and state.grids_block is None):
        return
    mapped_ok = {m.metric for m in state.metric_maps if m.series_id}
    weak = [dm for dm in state.demand["metrics"] if dm["metric"] not in mapped_ok]
    if weak:
        from app.population.rescue import rescue_metrics
        used_series = ({m.series_id for m in state.metric_maps if m.series_id}
                       | {sid for m in state.metric_maps for sid in (m.also_series_ids or [])})
        rescued = rescue_metrics(weak, state.catalogue, used_series=used_series,
                                 context=state.biz_context)
        if rescued:
            by = {m.metric: m for m in state.metric_maps}
            for rm in rescued:
                by[rm.metric] = rm     # per-metric deep opinion wins for the weak ones
            state.metric_maps = list(by.values())
            state.routing["rescue_attempted"] = len(weak)
            state.routing["rescue_placed"] = sum(1 for rm in rescued if rm.series_id)


def stage_conservation(state: RunState) -> None:
    """CONSERVATION LAW: no child of a partially-consumed source family may
    silently vanish (the missing share-based-payments class). Code finds the
    orphans from the source's own formula graph; one focused LLM call assigns
    each to a consuming bucket per the template's definitions (or excludes it
    with a reason); leftovers become a review question."""
    if not (state.catalogue and state.plan_stage != "final"):
        return
    with best_effort(state, "conservation pass"):
        from app.population.conservation import family_gaps, place_orphans
        gaps = family_gaps(state.source_snapshot, state.catalogue, state.metric_maps)
        if gaps:
            placed, excluded, leftovers = place_orphans(
                gaps, state.metric_maps, state.demand["metrics"], state.source_snapshot,
                context=state.biz_context)
            state.routing["conservation"] = {
                "families": len(gaps),
                "orphans": sum(len(g["orphans"]) for g in gaps),
                "placed": placed, "excluded": excluded,
                "unresolved": len(leftovers)}
            if leftovers:
                from app.review.items import make_item
                names = ", ".join(o["label"] for o in leftovers[:8])
                state.ask([make_item(
                    source="populate-plan", kind="judgment",
                    question=(f"{len(leftovers)} source component(s) of a partially-used "
                              f"total went unassigned ({names}) — where should they go?"),
                    why=("Siblings of these lines were mapped into the template, so their "
                         "amounts are part of a total the template shows — leaving them "
                         "out understates it."),
                    suggested_answer="tell me the template line to sum each into (or 'exclude')",
                )], priority=2)


def stage_aggregation(state: RunState) -> None:
    """Double-count guard scope: from the template's OWN formulas, which totals
    each metric feeds. Reuse of a source series is blocked only when two
    metrics share a total (would inflate it); a KPI mirrored across sheets
    feeds no shared total and fills freely. None (no formula graph) => the
    guard falls back to the conservative global block in verify_plan."""
    from app.population.aggregation import metric_totals
    try:
        state.agg_membership = metric_totals(state.target_inputs, state.t_snap)
    except Exception as e:  # noqa: BLE001 — never let graph analysis sink a fill
        logger.warning("aggregation membership failed (%s) — global double-count guard", e)
        state.agg_membership = None
    if state.agg_membership is not None:
        state.routing["totals_modelled"] = len(
            {t for ts in state.agg_membership.values() for t in ts})


def stage_contract_verify_execute(state: RunState) -> None:
    """FILL-PLAN PATH (docs/Fill-Plan-Architecture.md — the only path):
    plan → contract overlay → verify → one repair round → execute."""
    from app.population.contract import apply_decisions, load_decisions, source_fingerprint
    from app.population.mapping import repair_plan
    from app.population.verify import verify_plan

    state.src_fp = source_fingerprint(state.source_snapshot)
    state.decisions = load_decisions(state.t_vid, fingerprint=state.src_fp)
    n_dec = apply_decisions(state.metric_maps, state.decisions)
    if n_dec:
        state.routing["contract_decisions_applied"] = n_dec
    # Template-level scenario equivalence ({"budget": "forecast"}) — an
    # EXECUTOR rule from an answered decision, not a plan field: slots
    # demanding a scenario the source doesn't tag are served from its
    # user-confirmed equivalent (templates and sources routinely name the
    # same months differently; the wall only opens on an explicit answer).
    se = (state.decisions.get("*") or {}).get("scenario_equivalence")
    if isinstance(se, dict):
        state.scen_equiv = {str(k).strip().lower(): str(v).strip().lower()
                            for k, v in se.items() if k and v}
        if state.scen_equiv:
            state.routing["scenario_equivalence"] = state.scen_equiv
    progress.set_stage(state.target_template_id, "verifying")
    issues = verify_plan(state.metric_maps, state.catalogue, state.target_inputs,
                         state.demand, state.template_context, state.agg_membership,
                         scenario_equiv=state.scen_equiv)
    repairable: dict[str, list] = {}
    for i in issues:
        if i.severity == "repair":
            repairable.setdefault(i.metric, []).append(i)
    if repairable:
        by_fill = {m.metric: m for m in state.metric_maps}
        metric_by_key = {m["metric"]: m for m in state.demand["metrics"]}
        from app.population.schema import MetricMap as _MM
        failing = [(metric_by_key.get(mk) or {"metric": mk, "label": mk},
                    by_fill.get(mk) or _MM(metric=mk), iss)
                   for mk, iss in repairable.items()]
        repaired = repair_plan(failing, state.catalogue, context=state.biz_context,
                               grids=state.grids_block)
        if repaired:
            for rm in repaired:
                if rm.metric in repairable:
                    by_fill[rm.metric] = rm
            state.metric_maps = list(by_fill.values())
            state.routing["plan_repaired"] = len(repaired)
            issues = verify_plan(state.metric_maps, state.catalogue, state.target_inputs,
                                 state.demand, state.template_context, state.agg_membership,
                                 scenario_equiv=state.scen_equiv)
    for i in issues:            # unrepaired -> the question tier, never a dead end
        if i.severity == "repair":
            i.severity = "question"
    verify_and_execute(state, issues=issues)


def stage_revision_loop(state: RunState) -> None:
    """OUTCOME-DRIVEN REVISION (grid mode, one iteration): the model sees what
    actually HAPPENED to each problem metric — its own plan entry plus the
    executor's real per-metric blank reasons, with the grids still in context —
    and may revise only those. Revised entries re-run the FULL verify → execute
    chain: nothing bypasses the guards, and honest data gaps (missing
    periods/scenarios) are excluded so the model isn't asked to conjure data
    that isn't there."""
    if not (state.grids_block and state.links is not None and state.plan_stage != "final"):
        return
    with best_effort(state, "revision loop"):
        preview = apply_links(state.target_inputs, state.source_snapshot,
                              state.links, skipped=[])
        _HONEST = ("no source column for this period", "source has no budget",
                   "source has no forecast", "template ", "held for review")
        per_metric: dict[str, Counter] = {}
        for u in preview.unmatched:
            mk = u.get("metric")
            r = str(u.get("reason"))[:90]
            if mk and not any(h in r for h in _HONEST):
                per_metric.setdefault(mk, Counter())[r] += 1
        filled_keys = Counter(
            metric_key(f) for f in state.target_inputs
            if (f["sheet_name"], (f.get("cell") or "").upper())
            in {(lk.template_sheet, (lk.template_cell or "").upper()) for lk in state.links})
        by_plan = {m.metric: m for m in state.metric_maps}
        problems = [{
            "metric": mk,
            "plan": (by_plan[mk].model_dump(exclude_none=True) if mk in by_plan else {}),
            "outcome": (f"filled {filled_keys.get(mk, 0)} cells; blanks: "
                        + "; ".join(f"{n}x {r}" for r, n in list(ctr.items())[:4])),
        } for mk, ctr in per_metric.items()]
        if len(problems) >= 2:
            from app.population.contract import apply_decisions
            from app.population.mapping import revise_plan
            revised = revise_plan(problems[:20], state.catalogue, state.grids_block,
                                  context=state.biz_context)
            accepted = [rm for rm in revised if rm.metric in per_metric]
            if accepted:
                for rm in accepted:
                    by_plan[rm.metric] = rm
                state.metric_maps = list(by_plan.values())
                apply_decisions(state.metric_maps, state.decisions)   # the contract still wins
                verify_and_execute(state)
                state.routing["revision"] = {"problems": len(problems),
                                             "revised": len(accepted)}


def stage_tieout_loop(state: RunState) -> None:
    """TIE-OUT LOOP (the fundamental rule): render a PROBE of the current fill
    and evaluate the template's OWN check formulas BEFORE any deliverable
    exists. Failing tie-outs (EBITDA doesn't tie to the statutory P&L, the
    cash movement doesn't reconcile) mean something is mis-mapped/
    double-counted/missing — a workbook delivered with a different EBITDA than
    the company reports destroys the entire proposition. The model gets the
    exact failing checks + the grids and revises; ONE bounded iteration (a
    real run shipped with 32 of 84 checks failing and only a footnote). The
    final render then re-evaluates every check; anything still failing reports
    loudly in `tie_out` and the review inbox — never silently."""
    from app.population.pipeline.deliver import _calc_enabled, render_filled
    if not (state.grids_block and state.links is not None
            and state.template_check_cells and _calc_enabled()):
        return
    with best_effort(state, "tie-out loop"):
        probe_fill = apply_links(state.target_inputs, state.source_snapshot,
                                 state.links, skipped=[])
        _pb, _ps, _pa, _psk, probe_checks, _pl = render_filled(
            state.tgt_tmp, probe_fill.filled, state.target_inputs, reset=state.reset,
            checks=[dict(ch) for ch in state.template_check_cells])
        probe_failures = [r for r in probe_checks if r.get("status") == "fail"]
        state.routing["tie_out"] = {"failed_before": len(probe_failures)}
        if probe_failures:
            from app.population.contract import apply_decisions
            from app.population.mapping import revise_for_checks
            fixes = revise_for_checks(
                probe_failures, state.metric_maps, state.catalogue, state.grids_block,
                context=state.biz_context,
                flags=(state.routing.get("geometry_flags") or None))
            valid_keys = {m["metric"] for m in state.demand["metrics"]}
            accepted = [rm for rm in fixes if rm.metric in valid_keys][:15]
            if accepted:
                by_plan2 = {m.metric: m for m in state.metric_maps}
                for rm in accepted:
                    by_plan2[rm.metric] = rm
                state.metric_maps = list(by_plan2.values())
                apply_decisions(state.metric_maps, state.decisions)   # contract still wins
                verify_and_execute(state)
                state.routing["tie_out"]["revised"] = len(accepted)


def stage_final_plan_cache(state: RunState) -> None:
    """FINAL PLAN persisted: repeat runs of the same pair skip every LLM stage
    entirely (contract decisions still overlay fresh each run)."""
    if state.plan_key and state.metric_maps:
        try:
            source_cache.put(state.plan_key, {"stage": "final",
                                              "maps": [m.model_dump(mode="json")
                                                       for m in state.metric_maps]})
        except Exception:  # noqa: BLE001
            pass
    if state.plan_issues:
        state.routing["plan_issues"] = dict(Counter(i.code for i in state.plan_issues))
