"""The population pipeline: `_run_population` decomposed into named stages over
an explicit RunState (docs/Refactor-Population-Pipeline.md). The sequence here IS
the orchestration — read top to bottom; every stage is individually testable.

Behavior contract: identical to the pre-refactor monolith. LLM call gating,
cache reads/writes, progress stamps, review-item batching and the result dict
are unchanged; `run.py` keeps the public API.
"""

from __future__ import annotations

from app.population import progress
from app.population.cost import SpendGuard, default_cap_usd, set_guard
from app.population.periods import parse_any_date
from app.population.pipeline.state import RunState


def run_pipeline(state: RunState) -> dict:
    """Execute the population run described by ``state`` and return the result
    dict (or the dry-run estimate). Stage order is load-bearing — see each
    stage's docstring for what it consumes/produces on the state."""
    from app.population.pipeline import deliver, demand, estimate, plan, prepare, report, understand

    # Arm the spend firewall for this run (TEMPO_MAX_RUN_USD): every LLM call
    # inside checks against it and aborts before breaching.
    set_guard(SpendGuard(default_cap_usd()))
    progress.set_stage(state.target_template_id, "understanding")

    demand.stage_demand(state)
    # as-of is the pack's reporting vintage / timeline anchor only — it does NOT
    # classify actual vs forecast. Scenario is the source's own column tag, and no
    # source column is ever dropped for lacking an as-of; a time series carries data
    # before and after the as-of alike.
    state.as_of = parse_any_date(state.as_of_date)

    if state.dry_run:
        return estimate.consent_estimate(state)

    prepare.stage_load_template(state)
    try:
        prepare.stage_grids(state)
        understand.stage_plan_cache(state)
        understand.stage_claims_and_catalogue(state)
        understand.stage_routing_init(state)
        plan.stage_mapping(state)
        plan.stage_rescue(state)
        plan.stage_conservation(state)
        plan.stage_aggregation(state)
        plan.stage_contract_verify_execute(state)
        plan.stage_revision_loop(state)
        plan.stage_tieout_loop(state)
        plan.stage_final_plan_cache(state)
        report.stage_report(state)
        deliver.stage_additions(state)
        deliver.stage_render_upload(state)
        deliver.stage_check_report(state)
        deliver.stage_addition_items(state)
        deliver.stage_audit(state)
    finally:
        if state.tgt_tmp is not None:
            state.tgt_tmp.unlink(missing_ok=True)

    return deliver.build_result(state)
