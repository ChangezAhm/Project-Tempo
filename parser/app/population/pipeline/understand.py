"""Source understanding → catalogue: plan-cache read (resumability), the
one-pass understand+map call with its claims quality gate, and the two-pass
digest fallback. Everything the mapper will map FROM is decided here."""

from __future__ import annotations

import hashlib
import logging

from app.datamodel.derive import DERIVATION_VERSION
from app.population import source_cache
from app.population.catalogue import build_catalogue, catalogue_from_understanding
from app.population.cost import SpendCapExceeded
from app.population import progress
from app.population.pipeline.state import RunState
from app.population.source_understanding import cached_sheets, understand_source

logger = logging.getLogger(__name__)


def plan_key_for(content_hash: str | None, version_id: str | None) -> str | None:
    """THE plan-cache key — single source of truth, used by the live run and
    the dry-run estimate alike (they were two inline sha256 computations one
    format-string edit away from a silent cache-miss)."""
    if not (content_hash and version_id):
        return None
    # plan2: mapper contract gained coverage_series_ids (cross-sheet coverage) —
    # bump so plans cached under the old contract are re-mapped, not reused.
    return hashlib.sha256(
        f"{content_hash}|{version_id}|{DERIVATION_VERSION}|plan2".encode()).hexdigest()


def _build_source_catalogue(snapshot: dict, source_periods: dict, content_hash: str | None,
                            source_path=None, as_of=None):
    """Catalogue the source via AI understanding (robust to PortCo layout variance),
    falling back to deterministic detection if understanding yields nothing. A spend
    cap breach is never swallowed. ``source_path`` (the uploaded workbook on disk)
    lets understanding render sheet images for layout context. Every source column
    is kept — scenario is enforced later, in execute, and a budget column can only
    fill a slot that explicitly asks for budget; nothing is dropped by tag here.

    Returns (catalogue, source_kind, reconciliation_report)."""
    try:
        sheets = understand_source(snapshot, content_hash, source_path=source_path)
        # CODE AUDITS THE CLAIM: reconcile the AI's read against snapshot facts
        # (date headers, basis row, numeric rows) before anything consumes it —
        # a single under-enumerated answer must never silently narrow the fill.
        from app.population.reconcile import reconcile_source_understanding
        sheets, recon = reconcile_source_understanding(snapshot, sheets)
        geometry: list[str] = []
        cat = catalogue_from_understanding(snapshot, sheets, as_of=as_of,
                                           diagnostics=geometry)
        if geometry:
            # geometry problems (collapsed/long-format sheets) mean coverage is
            # KNOWN-partial — they ride the report, never disappear
            recon = {**recon, "geometry_flags": geometry}
            logger.warning("source geometry flags: %s", geometry)
        if cat:
            return cat, "ai_understanding", recon
        logger.warning("source understanding produced 0 series — falling back to deterministic detection")
    except SpendCapExceeded:
        raise
    except Exception:
        logger.exception("source understanding failed — falling back to deterministic detection")
    return build_catalogue(snapshot, source_periods), "deterministic_fallback", {}


# ---- stages ------------------------------------------------------------------

def stage_plan_cache(state: RunState) -> None:
    """PLAN CACHE (resumability): the mapping is the expensive, slow step — and
    an aborted run used to lose it entirely (a real test burned $40 across
    two cap-aborted runs that produced nothing). The plan is cached keyed by
    (source bytes, template version, derivation version); a re-run loads it,
    re-applies fresh contract decisions, and goes straight to verify/execute.
    'mapped' = saved right after mapping (abort insurance); 'final' = the
    end-of-run plan (repeat runs skip every loop; tie-out checks still run)."""
    state.plan_key = plan_key_for(state.content_hash, state.t_vid)
    if not state.plan_key:
        return
    pc = source_cache.get(state.plan_key)
    claims_c = cached_sheets(state.content_hash)
    if pc and pc.get("maps") and claims_c:
        try:
            from app.population.mapping import MetricMap as _MM2
            from app.population.reconcile import reconcile_source_understanding
            claims2, state.source_recon = reconcile_source_understanding(
                state.source_snapshot, [dict(s) for s in claims_c])
            cat_c = catalogue_from_understanding(state.source_snapshot, claims2,
                                                 as_of=state.as_of,
                                                 diagnostics=state.geometry_flags)
            maps_c = [_MM2(**m) for m in pc["maps"]]
            if cat_c and any(m.series_id for m in maps_c):
                state.catalogue, state.onepass_maps = cat_c, maps_c
                state.plan_cached, state.plan_stage = True, pc.get("stage", "final")
                state.catalogue_source = f"plan-cache:{state.plan_stage}"
        except Exception as e:  # noqa: BLE001 — a bad cache entry never blocks
            logger.warning("plan cache unusable (%s) — mapping fresh", e)
            state.catalogue = None
            state.onepass_maps = None


def stage_claims_and_catalogue(state: RunState) -> None:
    """ONE-PASS understand+map (grid mode), gated to templates the structured
    output can actually hold — a 231-metric template's one-pass reliably
    truncates, wasting a full Opus call before falling back. Big templates go
    straight to claims + batched grid mapping. Any failure → two-pass, loudly."""
    onepass_ok = (state.grids_block and not state.plan_cached
                  and len(state.demand["metrics"]) <= 80)
    if state.grids_block and not state.plan_cached and not onepass_ok:
        state.translate_notes.append(
            f"{len(state.demand['metrics'])} metrics — one-pass skipped (output too large), "
            "batched grid mapping used")
    if onepass_ok:
        try:
            from app.population.mapping import translate_sources, understand_and_map
            from app.population.reconcile import reconcile_source_understanding
            progress.set_stage(state.target_template_id, "planning",
                               f"{len(state.demand['metrics'])} metrics (one-pass)")
            claims, raw_maps, op_degraded = understand_and_map(
                state.demand["metrics"], state.grids_block,
                context=state.biz_context, images=state.grid_images)
            # CLAIMS QUALITY GATE: the one-pass structure dump can be squeezed
            # by its own output budget on big packs (a real run enumerated a
            # fraction of the periods and filled 261 of 3,418). The RICHEST
            # available claims win — the cache (a previous full understanding)
            # or a fresh per-sheet pass when degraded — while the grid-seeing
            # MAPPINGS are kept: label cells resolve against any claims.
            def _n_claims(sh_list):
                return sum(len(s.get("periods") or []) + len(s.get("series") or [])
                           for s in sh_list or [])
            gate_notes: list[str] = []
            cached_claims = cached_sheets(state.content_hash)
            if cached_claims and _n_claims(cached_claims) > 1.3 * _n_claims(claims):
                gate_notes.append(f"one-pass claims {_n_claims(claims)} < cached "
                                  f"{_n_claims(cached_claims)} — cache wins")
                claims = [dict(s) for s in cached_claims]
            elif op_degraded and not cached_claims:
                gate_notes.append("one-pass claims degraded — per-sheet understanding used")
                claims = understand_source(state.source_snapshot, state.content_hash,
                                           source_path=state.source_path)
            if gate_notes:
                logger.info("claims gate: %s", gate_notes)
            claims, state.source_recon = reconcile_source_understanding(
                state.source_snapshot, claims)
            sid_index: dict = {}
            cat = catalogue_from_understanding(state.source_snapshot, claims,
                                               as_of=state.as_of,
                                               diagnostics=state.geometry_flags,
                                               sid_index=sid_index)
            maps_t, tnotes = translate_sources(raw_maps, sid_index, set(cat))
            state.translate_notes = gate_notes + tnotes
            if cat and any(m.series_id for m in maps_t):
                state.catalogue, state.onepass_maps = cat, maps_t
                state.catalogue_source = "grid-onepass"
                if state.content_hash:   # claims still benefit dry-runs + fallbacks
                    try:
                        from app.population.source_understanding import _CACHE_VERSION
                        source_cache.put(state.content_hash,
                                         {"version": _CACHE_VERSION, "sheets": claims})
                    except Exception:  # noqa: BLE001 — cache is best-effort
                        pass
            else:
                raise RuntimeError("one-pass produced no usable catalogue/plan")
        except SpendCapExceeded:
            raise
        except Exception as e:  # noqa: BLE001 — two-pass fallback, loudly
            logger.warning("one-pass understand+map failed (%s) — two-pass fallback", e)
            state.catalogue = None
    if state.catalogue is None:
        state.catalogue, state.catalogue_source, state.source_recon = _build_source_catalogue(
            state.source_snapshot, state.source_periods, state.content_hash,
            state.source_path, state.as_of)


def stage_routing_init(state: RunState) -> None:
    """The routing block: everything code decided on the way in, never silent."""
    state.coverage_summary = _coverage_summary(state.catalogue, state.target_inputs)
    state.routing.update({
        "series": len(state.catalogue), "catalogue_source": state.catalogue_source,
        "mapper": ("grid-onepass" if state.onepass_maps is not None
                   else ("grid" if state.grids_block else "digest"))})
    if state.grid_notes:
        state.routing["grid_notes"] = state.grid_notes
    if state.grid_images:
        state.routing["grid_images"] = len(state.grid_images)
    if state.translate_notes:
        state.routing["translate_notes"] = state.translate_notes[:12]
    if state.geometry_flags:
        state.routing["geometry_flags"] = state.geometry_flags[:8]
    if state.source_recon:
        state.routing["source_reconciliation"] = state.source_recon   # what code patched, never silent
    if state.biz_context:
        state.routing["context_chars"] = len(state.biz_context)
    if state.demand.get("gated_cells"):
        state.routing["gated_cells"] = state.demand["gated_cells"]   # role-gated, never silent
    if state.demand.get("protected_totals"):
        state.routing["protected_totals"] = state.demand["protected_totals"]


def _coverage_summary(catalogue: dict, target_inputs: list[dict]) -> list[str]:
    """Plain-words date coverage: source span vs template demand — the missing
    sentence that made honest gaps ('this pack has no 2023') read as bugs."""
    from app.population.periods import parse_any_date
    out: list[str] = []
    try:
        src_dates = [d for s in catalogue.values() for (_c, d, _g) in s.period_cols if d]
        tpl_dates = [d for f in target_inputs
                     if (d := parse_any_date(f.get("parsed_date")))]
        if src_dates and tpl_dates:
            s_lo, s_hi = min(src_dates), max(src_dates)
            t_lo, t_hi = min(tpl_dates), max(tpl_dates)
            out.append(f"Source data covers {s_lo:%b-%Y} to {s_hi:%b-%Y}; "
                       f"the template asks for {t_lo:%b-%Y} to {t_hi:%b-%Y}.")
            if t_lo < s_lo:
                n = (s_lo.year - t_lo.year) * 12 + (s_lo.month - t_lo.month)
                out.append(f"The template's first ~{n} monthly column(s) per metric predate "
                           "this source — those blanks are missing data, not mapping failures.")
            if t_hi > s_hi:
                n = (t_hi.year - s_hi.year) * 12 + (t_hi.month - s_hi.month)
                out.append(f"The template's last ~{n} monthly column(s) postdate the "
                           "source's newest data.")
        by_sheet: dict[str, list] = {}
        for s in catalogue.values():
            for (_c, d, _g) in s.period_cols:
                if d:
                    by_sheet.setdefault(s.sheet, []).append(d)
        if len(by_sheet) > 1:
            spans = ", ".join(f"{sh} {min(ds):%b-%y}..{max(ds):%b-%y}"
                              for sh, ds in sorted(by_sheet.items()))
            out.append(f"Source sheets span different ranges ({spans}) — each metric "
                       "inherits its source sheet's range.")
    except Exception:  # noqa: BLE001 — a summary must never sink a run
        pass
    return out
