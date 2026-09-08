"""Delivery: refresh-then-fill rendering (values + traceable variants),
add-line additions, storage uploads, the template's own check verdict, the
persisted audit, and the result dict — the response contract the frontend
consumes (keys/truncation limits are load-bearing)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from app import supabase_client as sb
from app.population.pipeline.state import RunState

logger = logging.getLogger(__name__)


def _is_clearable_value(value, is_formula: bool) -> bool:
    """Stale data to wipe on a standard refresh = a plain NUMBER sitting in an
    input cell. Never clear a formula (computed/connector cell) or text (a
    label/header) — only numeric literals, so structure stays untouched."""
    if is_formula:
        return False
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _calc_enabled() -> bool:
    return os.environ.get("TEMPO_VERIFY_CALC", "1").lower() not in ("0", "false", "off")


def render_filled(template_workbook_path, filled, clear_facts=(), *, reset: str = "values",
                  additions=None, sval=None, checks=None,
                  links=None, source_snapshot=None, source_path=None,
                  link_sources: bool = False,
                  ) -> tuple[bytes, dict, list, list, list, bytes | None]:
    """Refresh-then-fill on a COPY of the template (the stored master is never
    touched). ``clear_facts`` = the in-scope input facts (data/sourced only —
    computed formulas are never in scope).

    reset="values": wipe stale numeric LITERALS only (formulas keep their cached
      values — on connector templates that leaves the previous company's numbers
      visible in unfilled cells).
    reset="full": ONE FILE, ONE COMPANY — clear the contents of EVERY in-scope
      input cell, including connector formulas (the dropped file substitutes for
      the connector in this copy), so an unfilled cell is visibly empty, never
      another company's number.

    ``additions``: approved add-line proposals, written via authoring.apply_additions
    (needs ``sval``, the source value map).

    ``checks``: template check cells (template_checks.collect_check_cells). After
    the deliverable bytes are captured, the workbook is RECALCULATED and each
    check read back — "fill it and see what the template itself says". The bytes
    are saved BEFORE calc because a recalc turns connector cells (CX_GET…) into
    #NAME? which must never reach the delivered file.

    ``link_sources``: also render the TRACEABLE deliverable — the same fill with
    each written value replaced by a formula referencing the source data, which
    is imported as values-only "Source - …" sheets (population.linked). Runs on
    the same in-memory workbook AFTER the values bytes are frozen and BEFORE the
    checks recalc (the formulas are verified to compute the same values, so the
    checks read identically). Needs ``links`` + ``source_snapshot`` (+ optional
    ``source_path`` for formatting-true sheet copies). Best-effort: a linked
    render failure is reported in stats, never sinks the fill.

    Returns (bytes, clear_stats, additions_applied, additions_skipped,
    check_results, linked_bytes)."""
    from aspose.cells import Workbook
    wb = Workbook(str(template_workbook_path))
    ws_by_name = {w.name: w for w in wb.worksheets}

    # authoritative BEFORE values for the checks, from the same live workbook
    for ch in checks or []:
        ws = ws_by_name.get(ch.get("sheet"))
        if ws is not None:
            ch["before"] = ws.cells.get(ch["cell"]).value

    cleared_values = cleared_formulas = 0
    # Cells a match will actually refill — a 'sourced' (connector-fed / system-fed)
    # cell holds real CURRENT company data, so it is cleared ONLY when a mapped
    # value will replace it. Otherwise a connector cell with no source match (e.g.
    # one still awaiting period/scenario resolution) would be wiped and left blank,
    # destroying the last-fetched financials. Manual 'data' inputs keep the stale-
    # wipe (an unfilled input should read empty, not show a prior company's number).
    fill_targets = {(fc.template_sheet, fc.template_cell) for fc in filled}
    for f in clear_facts:
        ws = ws_by_name.get(f.get("sheet_name"))
        if ws is None or not f.get("cell"):
            continue
        if ((f.get("category") == "sourced" or f.get("write_mode") == "type_over")
                and (f.get("sheet_name"), f["cell"]) not in fill_targets):
            continue   # an unfilled sourced/type-over cell keeps its default (formula or value)
        cell = ws.cells.get(f["cell"])
        if _is_clearable_value(cell.value, cell.is_formula):
            ws.cells.clear_contents(cell.row, cell.column, cell.row, cell.column)
            cleared_values += 1
        elif reset == "full" and cell.is_formula:
            ws.cells.clear_contents(cell.row, cell.column, cell.row, cell.column)
            cleared_formulas += 1

    write_failures: list[dict] = []
    for fc in filled:
        ws = ws_by_name.get(fc.template_sheet)
        if ws is not None:
            ws.cells.get(fc.template_cell).put_value(fc.value)
        else:
            # a filled value whose sheet is missing from the workbook must never
            # vanish silently — record it so the run report shows the loss.
            write_failures.append({"template_sheet": fc.template_sheet,
                                   "template_cell": fc.template_cell,
                                   "reason": "sheet not found in workbook"})

    applied, skipped = [], []
    if additions:
        from app.population.authoring import apply_additions
        applied, skipped = apply_additions(ws_by_name, additions, sval or {})

    # The deliverable is frozen without a workbook recalc, so every dependent
    # formula still carries its pre-fill cached value. Excel trusts those caches
    # unless the file demands a full calculation on open — without this flag the
    # user sees stale numbers until they F2 each cell.
    wb.settings.formula_settings.calculate_on_open = True

    fd, name = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    out = Path(name)
    try:
        wb.save(str(out))
        data = out.read_bytes()          # deliverable frozen BEFORE any recalc
        stats = {"cleared_values": cleared_values, "cleared_formulas": cleared_formulas}
        if write_failures:
            stats["write_failures"] = write_failures[:50]

        # TRACEABLE variant: same workbook, values swapped for verified formulas
        # + imported source sheets. Frozen before the checks calc, like `data`.
        linked_bytes: bytes | None = None
        if link_sources and links and filled:
            try:
                from app.population.linked import attach_source_links
                lstats = attach_source_links(wb, filled, links,
                                             source_snapshot or {}, source_path)
                if applied:
                    # add-line writes are values in both deliverables (for now)
                    lstats["additions_not_linked"] = len(applied)
                wb.save(str(out))
                linked_bytes = out.read_bytes()
                stats["linked"] = lstats
            except Exception as e:  # noqa: BLE001 — traceable render must never sink the fill
                logger.warning("linked-workbook render failed: %s", e)
                stats["linked_error"] = str(e)[:200]

        check_results: list = []
        if checks and _calc_enabled():
            try:
                from time import monotonic

                from aspose.cells import CalculationOptions

                from app.population.template_checks import evaluate_checks
                opts = CalculationOptions()
                opts.ignore_error = True
                t0 = monotonic()
                wb.calculate_formula(opts)
                stats["calc_seconds"] = round(monotonic() - t0, 2)
                check_results = evaluate_checks(wb, checks)
            except Exception as e:  # noqa: BLE001 — verification must never sink the fill
                logger.warning("post-fill recalculation failed: %s", e)
                stats["calc_error"] = str(e)[:200]
        return data, stats, applied, skipped, check_results, linked_bytes
    finally:
        out.unlink(missing_ok=True)


# ---- stages ------------------------------------------------------------------

def stage_additions(state: RunState) -> None:
    """Add-line additions: source series that mapped to NO template metric are
    routed into the template's extensible regions (the BRIDGE: a region whose
    hosted metric came back unavailable activates with ranked candidates).
    Default is "apply": blank-slot lines write immediately (flagged + filed
    in the inbox); occupied-label overwrites are approval-gated one-tap items,
    and previously-approved ones replay automatically."""
    if state.add_lines in ("propose", "apply"):
        try:
            from app.population.region_bridge import (
                approved_addition_proposals, route_additions)
            regions = sb.list_extensible_regions(state.t_vid)
            if regions:
                # NOVELTY, not just unusedness: series the source's own graph
                # derives from consumed rows (subtotals/margins over them) or
                # that sit inside a consumed total duplicate what's already
                # filled — excluded so genuinely new data (KPIs) ranks first.
                from app.population.conservation import (
                    _label_tokens, redundant_series)
                from app.population.region_bridge import used_series as _used
                _uset = _used(state.metric_maps)
                # lines the fill already displays: used series' labels +
                # the template's own metric labels (a derivable series is
                # redundant only when it duplicates one of these)
                display = ({_label_tokens(state.catalogue[sid].label)
                            for sid in _uset if sid in state.catalogue}
                           | {_label_tokens(m.get("label") or m.get("metric"))
                              for m in state.demand["metrics"]})
                redundant = redundant_series(state.source_snapshot, state.catalogue,
                                             _uset, display_labels=display)
                if redundant:
                    state.routing["additions_redundant_excluded"] = len(redundant)
                state.proposals, state.add_notes = route_additions(
                    state.catalogue, state.metric_maps, regions, state.target_inputs,
                    redundant=redundant)
                state.proposals.extend(approved_addition_proposals(state.t_vid))
            else:
                state.add_notes = ["no extensible regions stored — re-run Understand (or region detection) first"]
        except Exception as e:  # noqa: BLE001 — additions must never sink the fill
            logger.exception("add-line proposal failed")
            state.add_notes = [f"add-line proposals unavailable: {e}"]

    def _writable(p: dict) -> bool:
        # blank slots and PLACEHOLDER slots write immediately — a throwaway
        # label ('Custom KPI 1') is the area's designed invitation, and
        # every write is logged + filed as a reversible "keep it?" item.
        # Only REAL labels (editable_label) stay approval-gated.
        return ((p.get("slot_mode") or "blank") in ("blank", "placeholder")
                or p.get("approved") is True)

    state.writable_proposals = ([p for p in state.proposals if _writable(p)]
                                if state.add_lines == "apply" else [])
    state.pending_overwrites = [p for p in state.proposals if not _writable(p)]


def stage_render_upload(state: RunState) -> None:
    """refresh-then-fill on the copy: wipe stale data across ALL in-scope
    inputs (per the reset mode), write the matches, then the writable
    additions — so uncovered inputs end up empty, not stale."""
    from app.population import progress
    from app.population.catalogue import effective_value
    try:
        if state.writable_proposals:
            for s in state.source_snapshot.get("sheets", []):
                for c in s.get("cells", []):
                    a = (c.get("address") or "").upper()
                    if a:
                        state.sval[(s["name"], a)] = effective_value(c)
        progress.set_stage(state.target_template_id, "writing")
        (filled_bytes, state.clear_stats, state.additions_applied, state.additions_skipped,
         state.check_results, linked_bytes) = render_filled(
            state.tgt_tmp, state.result.filled, state.target_inputs, reset=state.reset,
            additions=(state.writable_proposals or None), sval=state.sval,
            checks=state.template_check_cells,
            links=state.links, source_snapshot=state.source_snapshot,
            source_path=state.source_path, link_sources=state.link_sources)
        filled_path = sb.upload_filled(state.t_vid, state.source_label, filled_bytes,
                                       run_stamp=state.run_stamp)
        state.filled_url = sb.signed_filled_url(filled_path)
        state.linked_stats = state.clear_stats.get("linked") or {}
        if state.clear_stats.get("linked_error"):
            state.linked_stats = {"error": state.clear_stats["linked_error"]}
        if linked_bytes:
            linked_path = sb.upload_filled(state.t_vid, state.source_label, linked_bytes,
                                           variant="linked", run_stamp=state.run_stamp)
            state.linked_url = sb.signed_filled_url(linked_path)
    except Exception as e:  # noqa: BLE001 — render failure shouldn't lose the mapping/report
        logger.warning("filled-workbook render/upload failed: %s", e)
        state.routing.setdefault("stage_warnings", []).append(
            f"render/upload: {str(e)[:160]}")


def stage_check_report(state: RunState) -> None:
    """The template's own verdict: recalculated check cells. Failures are
    surfaced in the run AND filed as durable review questions."""
    if not state.check_results:
        return
    from app.population.template_checks import summarize_checks
    state.template_checks = {**summarize_checks(state.check_results),
                             "items": state.check_results[:100],
                             "calc_seconds": state.clear_stats.get("calc_seconds")}
    failed_checks = [r for r in state.check_results if r.get("status") == "fail"]
    for r in failed_checks[:50]:
        state.review.append({"template_sheet": r["sheet"], "template_cell": r["cell"],
                             "note": f"template check FAILED: {r['label']} "
                                     f"({str(r.get('before'))[:24]} → {str(r.get('after'))[:24]})"})
    if failed_checks:
        from app.review.items import make_item
        cc = [f"{r['sheet']}!{r['cell']}" for r in failed_checks]
        state.ask([make_item(
            source="populate", kind="judgment",
            question=(f"{len(cc)} of the template's own validation checks read FAIL after "
                      f"this fill ({', '.join(cc[:5])}{'…' if len(cc) > 5 else ''}) — "
                      "accept the fill anyway?"),
            why="; ".join(f"{r['sheet']}!{r['cell']}: {str(r.get('before'))[:24]!r}"
                          f" → {str(r.get('after'))[:24]!r}" for r in failed_checks[:10]),
            affected={"cells": cc[:20]},
            suggested_answer="yes — accept; I'll review the check cells in the workbook",
        )], priority=4)


def stage_addition_items(state: RunState) -> None:
    """Additions into the inbox: written lines as informational "keep it?" items,
    occupied-label proposals as one-tap approvals (approving replays next run)."""
    if state.additions_applied or state.pending_overwrites:
        try:
            from app.population.region_bridge import addition_review_items
            state.ask(addition_review_items(state.additions_applied,
                                            state.pending_overwrites,
                                            state.source_label), priority=6)
        except Exception as e:  # noqa: BLE001 — inbox filing must never fail the run
            logger.warning("could not file addition review items: %s", e)


def stage_audit(state: RunState) -> None:
    """Persist the full audit (demand, routing, every link, skipped, unmatched)."""
    try:
        audit = {
            "target_template_id": state.target_template_id,
            "source_filename": state.source_label,
            "as_of_date": state.as_of_date, "demand": state.demand,
            "routing": state.routing,
            "links": [lk.model_dump(mode="json") for lk in state.links],
            "filled": [fc.model_dump(mode="json") for fc in state.result.filled],
            "unmatched": state.result.unmatched, "unmatched_reasons": state.unmatched_reasons,
            "skipped": state.result.skipped,
            "review": state.review, "notes": state.notes,
            "coverage_notes": state.coverage_notes,
            "reconciled": state.reconciled_metrics,
            "unused_source_series": state.unused_source_series,
            "template_checks": state.template_checks,
            "summary": state.result.summary,
            "rule_violations": state.violations, "context_chars": len(state.biz_context),
            "fill_plan": [m.model_dump(exclude_none=True) for m in state.metric_maps],
            "plan_issues": [i.model_dump(exclude_none=True) for i in state.plan_issues],
            "reset": state.reset, **state.clear_stats,
            "linked": state.linked_stats,
            "proposed_additions": state.proposals, "addition_notes": state.add_notes,
            "additions_applied": state.additions_applied,
            "additions_skipped": state.additions_skipped,
        }
        audit_path = sb.upload_audit(state.t_vid, state.source_label,
                                     json.dumps(audit, default=str).encode(),
                                     run_stamp=state.run_stamp)
        state.audit_url = sb.signed_filled_url(audit_path)
    except Exception as e:  # noqa: BLE001 — audit is best-effort
        logger.warning("audit upload failed: %s", e)
        state.routing.setdefault("stage_warnings", []).append(
            f"audit upload: {str(e)[:160]}")


def build_result(state: RunState) -> dict:
    """The response contract. Keys, order and truncation limits are consumed by
    the frontend (PopulatePanel) and the benchmarks — change them deliberately
    or not at all."""
    filled = [fc.model_dump(mode="json") for fc in state.result.filled]
    try:
        from app.review.items import file_questions
        state.routing["questions"] = file_questions(state.t_vid, state.pending_q,
                                                    family="populate")
    except Exception as e:  # noqa: BLE001 — inbox filing must never fail the run
        logger.warning("question filing failed: %s", e)

    clear_stats = state.clear_stats
    return {
        "target_template_id": state.target_template_id,
        "source_filename": state.source_label,
        "as_of_date": state.as_of_date,
        "demand_metrics": len(state.demand["metrics"]),
        "summary": state.result.summary,
        "routing": state.routing,
        "links_count": len(state.links),
        "filled": filled[:500],
        "filled_truncated": len(filled) > 500,
        "unmatched": state.result.unmatched[:200],
        "unmatched_truncated": len(state.result.unmatched) > 200,
        "unmatched_count": len(state.result.unmatched),
        "unmatched_reasons": state.unmatched_reasons,
        "coverage_summary": state.coverage_summary,
        "unmapped_metrics": state.unmapped_metrics[:60],
        "skipped": state.result.skipped[:200],
        "skipped_truncated": len(state.result.skipped) > 200,
        "skipped_count": len(state.result.skipped),
        "review": state.review[:200],
        "review_truncated": len(state.review) > 200,
        "review_count": len(state.review),
        "coverage_notes": state.coverage_notes,
        "reconciled": state.reconciled_metrics,
        "reconciled_count": len(state.reconciled_metrics),
        "open_questions": state.open_questions,
        "open_questions_count": len(state.open_questions),
        "unused_source_series": state.unused_source_series,
        "template_checks": state.template_checks,
        # THE verdict a reviewer must see first: did the template's own
        # tie-outs pass? failed_after > 0 = the workbook disagrees with the
        # company's own reported totals — flagged red, never a footnote.
        "tie_out": {**(state.routing.get("tie_out") or {}),
                    "evaluated": state.template_checks.get("evaluated"),
                    "failed_after": state.template_checks.get("failed")}
        if state.template_checks else None,
        "rule_violations": state.violations[:100],
        "rule_violation_count": len(state.violations),
        "reset": state.reset,
        "cleared_count": clear_stats.get("cleared_values", 0) + clear_stats.get("cleared_formulas", 0),
        "cleared_values": clear_stats.get("cleared_values", 0),
        "cleared_formulas": clear_stats.get("cleared_formulas", 0),
        "proposed_additions": state.proposals[:100],
        "pending_label_overwrites": state.pending_overwrites[:50],
        "addition_notes": state.add_notes[:20],
        "additions_applied": state.additions_applied[:100],
        "additions_skipped": state.additions_skipped[:50],
        "notes": state.notes,
        "fill_plan": [m.model_dump(exclude_none=True) for m in state.metric_maps],
        "plan_issues": [i.model_dump(exclude_none=True) for i in state.plan_issues][:100],
        "filled_url": state.filled_url,
        "linked_url": state.linked_url,
        "linked": state.linked_stats,
        "audit_url": state.audit_url,
    }
