"""CONSENT ESTIMATE: price the run the way it will ACTUALLY execute — grid
mode with real grids, cache states, plan-cache hits — BEFORE a token is spent.
(The old digest-based estimate undershot a real run by 10x, and the mid-run
cap abort burned $40 across two attempts.)"""

from __future__ import annotations

import os

from app import supabase_client as sb
from app.population.catalogue import catalogue_from_understanding
from app.population.context import load_context
from app.population.cost import default_cap_usd
from app.population.mapping import estimate_mapping_usd
from app.population.pipeline.prepare import (_build_grid_block, _load_template_snap,
                                             _reset_preview)
from app.population.pipeline.state import RunState
from app.population.pipeline.understand import plan_key_for
from app.population.source_cache import get as cache_get
from app.population.source_understanding import (cached_sheets,
                                                 estimate_source_understanding_usd)


def consent_estimate(state: RunState) -> dict:
    cached = cached_sheets(state.content_hash)
    if cached is not None:
        catalogue = catalogue_from_understanding(state.source_snapshot, cached,
                                                 as_of=state.as_of)
        src_est, src_state = 0.0, "cached"
    else:
        catalogue = {}
        src_est, src_state = estimate_source_understanding_usd(state.source_snapshot), "would_run"
    ctx = ""
    t_snap_d = None
    try:
        preview_vid, _, _ = sb.get_latest_file(state.target_template_id)
        reset_preview = _reset_preview(preview_vid, state.target_inputs)
        ctx = load_context(state.target_template_id, preview_vid)
        t_snap_d = _load_template_snap(preview_vid)
    except Exception:  # noqa: BLE001 — preview is best-effort
        reset_preview = {}
        preview_vid = None
    plan_hit = None
    pk = plan_key_for(state.content_hash, preview_vid)
    if pk:
        pc = cache_get(pk)
        plan_hit = pc.get("stage") if pc and pc.get("maps") else None
    grids_est = None
    if os.environ.get("TEMPO_MAPPER", "grid").lower() == "grid":
        try:
            grids_est, _n, _d = _build_grid_block(t_snap_d, state.target_inputs,
                                                  state.source_snapshot)
        except Exception:  # noqa: BLE001
            grids_est = None
    map_est = (0.0 if plan_hit else
               (estimate_mapping_usd(state.demand["metrics"], catalogue, context=ctx,
                                     grids=grids_est) if catalogue else None))
    # loops allowance: revision + tie-out iterations when checks/blanks bite
    loops_est = 0.0 if plan_hit == "final" else 3.0
    total = round(src_est + (map_est or 0.0) + loops_est, 2)
    return {
        "dry_run": True, "target_template_id": state.target_template_id,
        "source_filename": state.source_label,
        "demand_metrics": len(state.demand["metrics"]),
        "input_cells_to_fill": len(state.target_inputs),
        "source_understanding": src_state,
        "source_series": len(catalogue),
        "plan_cache": plan_hit,
        "estimated_source_understanding_usd": src_est,
        "estimated_mapping_usd": map_est,
        "estimated_total_usd": total,
        "cap_exceeded": total > default_cap_usd(),
        "run_cap_usd": default_cap_usd(),
        "reset_preview": reset_preview,
        "context_chars": len(ctx),
    }
