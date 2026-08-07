"""Population orchestrator (Build A): parse-source is reused upload+parse, then
match (LLM) → apply (deterministic) → render filled workbook + attribution.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from app import supabase_client as sb
from app.datamodel.derive import DERIVATION_VERSION
from app.datamodel.persist import derive_and_persist, get_data_model
from app.population import source_cache
from app.population.apply import apply_links
from app.population.catalogue import build_catalogue, catalogue_from_understanding, effective_value
from app.population.periods import parse_any_date, sheet_grains
from app.population.schema import metric_key
from app.population.cost import SpendCapExceeded, SpendGuard, default_cap_usd, set_guard
from app.population import progress
from app.population.context import load_context
from app.population.mapping import estimate_mapping_usd, map_metrics
from app.population.source_understanding import (
    cached_sheets, estimate_source_understanding_usd, understand_source,
)
from app.raw_extraction.workbook_parser import parse_workbook
from app.snapshot import workbook_to_snapshot
from app.structure.detect import detect_structure

logger = logging.getLogger(__name__)

_ROWN = re.compile(r"^row \d+$")   # a pure positional fallback label (no metric identity)


def build_demand(template_id: str, as_of_date: str | None) -> tuple[dict, list[dict]]:
    """The template's 'demand' (distinct input metrics + period/scenario shape) +
    the input facts to fill. Targets the data/sourced cells (the actual inputs),
    not computed/config/exclude."""
    dm = get_data_model(template_id, limit=30000)
    stored_ver = ((dm.get("model") or {}).get("dimensions") or {}).get("derivation_version", 0)
    # Auto-(re)derive when the data model is missing OR was built by older logic, so
    # population always uses an up-to-date map (deterministic; no separate step).
    if not dm.get("available") or stored_ver < DERIVATION_VERSION:
        logger.info("data model for %s missing/stale (v%s < v%s) — (re)deriving now",
                    template_id, stored_ver, DERIVATION_VERSION)
        try:
            derive_and_persist(template_id)
        except Exception as e:  # noqa: BLE001
            if not dm.get("available"):
                raise RuntimeError(
                    f"Target has no data model and it can't be derived — run 'Understand' on the target first. ({e})"
                )
            logger.warning("re-derive failed (%s) — using the existing (stale) data model", e)
        dm = get_data_model(template_id, limit=30000)
        if not dm.get("available"):
            raise RuntimeError("Could not build a data model for the target.")
    fillable = [f for f in dm["facts"] if f.get("category") in ("data", "sourced")]
    # value_role guard: a total/subtotal/header row is the TEMPLATE'S arithmetic —
    # even when its cells are literals it must be neither cleared nor written.
    _ROLE_PROTECTED = ("total", "subtotal", "header")
    inputs = [f for f in fillable
              if (f.get("value_role") or "").strip().lower() not in _ROLE_PROTECTED]
    protected_totals = len(fillable) - len(inputs)
    # sheet-role write gate accounting — how many input-looking cells were blocked
    # (category='staging'); surfaced so a smaller fill explains itself.
    gated_cells = sum(1 for f in dm["facts"] if f.get("category") == "staging")
    metrics: dict[str, dict] = {}
    for f in inputs:
        key = metric_key(f)
        # A pure positional fallback label ('row 25') carries no meaning for the
        # mapper — it can never match a source series by name, so it would only
        # waste a mapping slot and pollute the report. The cell stays a fact (it's
        # still an input in the model); it just doesn't generate mapping demand
        # until it earns a real metric identity (e.g. via grid understanding).
        if key and _ROWN.match(key):
            continue
        if key and key not in metrics:
            # definition/qualification_criteria/expected_source are the L3 business
            # logic — the mapper needs them to tell 'Adjusted' from 'Reported', to
            # refuse a series that fails the template's own qualification rules,
            # and to prefer/refuse a source of the wrong provenance.
            metrics[key] = {"metric": key, "label": f.get("metric_label"), "unit": f.get("unit"),
                            "sign_convention": f.get("sign_convention"),
                            "definition": f.get("definition"),
                            "qualification_criteria": f.get("qualification_criteria"),
                            "expected_source": f.get("expected_source")}
    # period_index is a PER-SHEET ordinal, so the count used for positional
    # alignment must be per-sheet too — a global max would misalign sheets whose
    # timelines are shorter than the longest one in the workbook.
    period_count_by_sheet: dict[str, int] = {}
    for f in inputs:
        if f.get("period_index") is not None:
            s = f["sheet_name"]
            period_count_by_sheet[s] = max(period_count_by_sheet.get(s, 0), f["period_index"] + 1)
    period_count = max(period_count_by_sheet.values(), default=0)
    scenarios = sorted({f["scenario"] for f in inputs if f.get("scenario") and f["scenario"] != "unknown"})
    # DOMINANT grain, not alphabetical: a single YTD/annual column used to make
    # sorted()[0] say 'annual' for a monthly template, sending the dateless
    # positional-alignment fallback hunting for year columns.
    grain_votes = Counter(f.get("period_type") for f in inputs if f.get("period_type"))
    stored = (dm["model"] or {}).get("period_grains") or ["monthly"]
    period_grain = grain_votes.most_common(1)[0][0] if grain_votes else stored[0]
    demand = {"as_of_date": as_of_date, "period_count": period_count,
              "period_count_by_sheet": period_count_by_sheet,
              "period_grain": period_grain,
              "scenarios": scenarios, "metrics": list(metrics.values()),
              "gated_cells": gated_cells, "protected_totals": protected_totals}
    return demand, inputs


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
                  additions=None, sval=None, checks=None) -> tuple[bytes, dict, list, list, list]:
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

    Returns (bytes, clear_stats, additions_applied, additions_skipped, check_results)."""
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

    fd, name = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    out = Path(name)
    try:
        wb.save(str(out))
        data = out.read_bytes()          # deliverable frozen BEFORE any recalc
        stats = {"cleared_values": cleared_values, "cleared_formulas": cleared_formulas}
        if write_failures:
            stats["write_failures"] = write_failures[:50]

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
        return data, stats, applied, skipped, check_results
    finally:
        out.unlink(missing_ok=True)


def _download_to_temp(storage_path: str, filename: str) -> Path:
    data = sb.download_workbook(storage_path)
    fd, name = tempfile.mkstemp(suffix=Path(filename).suffix or ".xlsx")
    os.close(fd)
    p = Path(name)
    p.write_bytes(data)
    return p


def _bytes_to_temp(filename: str, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(suffix=Path(filename).suffix or ".xlsx")
    os.close(fd)
    p = Path(name)
    p.write_bytes(data)
    return p


def _pick_timeline(row_dates: dict[int, dict[int, object]]) -> dict[int, object]:
    """The row that is the sheet's period header: prefer rows whose dates increase
    left→right (a real timeline). A DATA row whose values happen to fall in the
    Excel date-serial range (29k–60k, e.g. thousands-scale amounts) parses as
    'dates' too, but financial series aren't monotonic by column — this stops such
    a row from hijacking the timeline and garbling date alignment."""
    def monotonic(d: dict[int, object]) -> bool:
        vals = [d[c] for c in sorted(d)]
        return all(a <= b for a, b in zip(vals, vals[1:]))

    mono = [d for d in row_dates.values() if len(d) >= 3 and monotonic(d)]
    pool = mono or list(row_dates.values())
    return max(pool, key=len)


def _load_template_snap(version_id: str) -> dict | None:
    """The template's stored snapshot, or None. Best-effort — populate degrades
    (no scale/date context, no check discovery) rather than failing."""
    try:
        return json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    except Exception as e:  # noqa: BLE001
        logger.info("template snapshot unavailable (%s)", e)
        return None


def _template_context(snap: dict | None) -> tuple[dict, dict, dict]:
    """From the template snapshot, the maps verify/execute need:
      - numfmt[(sheet, A1)]      -> number format (kind/currency for scale)
      - mags[(sheet, row)]       -> numeric magnitudes already in the row (scale by
                                    what the cell holds, not by its unit label)
      - dates_by_col[(sheet,col)]-> the column's real period date, read from the
                                    sheet's timeline header row, so periods align
                                    by actual date.
    Best-effort — empty on any failure (execute then uses label scale + positional)."""
    numfmt: dict[tuple[str, str], str] = {}
    mags: dict[tuple[str, int], list[float]] = defaultdict(list)
    dates_by_col: dict[tuple[str, int], object] = {}
    if not snap:
        return {}, {}, {}
    for s in snap.get("sheets", []):
        name = s.get("name")
        row_dates: dict[int, dict[int, object]] = defaultdict(dict)   # row -> {col: date}
        for c in s.get("cells", []):
            addr = (c.get("address") or "").upper()
            nf = (c.get("style") or {}).get("number_format")
            if addr and nf:
                numfmt[(name, addr)] = nf
            ev = effective_value(c)
            if isinstance(ev, (int, float)) and not isinstance(ev, bool) and ev:
                mags[(name, c.get("row"))].append(float(ev))
            d = parse_any_date(ev)
            if d is not None:
                row_dates[c["row"]][c["col"]] = d
        if row_dates:
            for col, d in _pick_timeline(row_dates).items():
                dates_by_col[(name, col)] = d
    return numfmt, dict(mags), dates_by_col


def _attach_slot_facts(metrics: list[dict], facts: list[dict],
                       template_context: tuple[dict, dict, dict]) -> None:
    """Attach per-metric SLOT FACTS for the planner: which sheets demand this
    metric, at what grain, over what date range, and whether the rows already
    hold numbers (scale evidence). Facts only — the planner judges from them."""
    _numfmt, mags_by_row, dates_by_col = template_context or ({}, {}, {})
    sheet_dates: dict[str, list] = {}
    for (sh, _c), d in dates_by_col.items():
        sheet_dates.setdefault(sh, []).append(d)
    grains = sheet_grains(dates_by_col)
    per_metric: dict[str, dict[str, int]] = {}
    has_values: dict[str, bool] = {}
    for f in facts:
        k = metric_key(f)
        if not k:
            continue
        sh = f.get("sheet_name")
        per_metric.setdefault(k, {})[sh] = per_metric.get(k, {}).get(sh, 0) + 1
        if mags_by_row.get((sh, f.get("row"))):
            has_values[k] = True
    for m in metrics:
        sheets = per_metric.get(m.get("metric"), {})
        if not sheets:
            continue
        parts = []
        for sh, n in sheets.items():
            ds = sorted(sheet_dates.get(sh, []))
            if ds:
                g = grains.get(sh) or "?"
                parts.append(f"{sh}: {g}ly {ds[0]:%Y-%m}..{ds[-1]:%Y-%m} ({n} slots)")
            else:
                parts.append(f"{sh}: undated ({n} slots)")
        summary = "; ".join(parts)
        if not has_values.get(m.get("metric")):
            summary += " — rows currently empty"
        m["slots"] = summary


def _build_source_catalogue(snapshot: dict, source_periods: dict, content_hash: str | None,
                            source_path: Path | None = None, as_of: date | None = None):
    """Catalogue the source via AI understanding (robust to PortCo layout variance),
    falling back to deterministic detection if understanding yields nothing. A spend
    cap breach is never swallowed. ``source_path`` (the uploaded workbook on disk)
    lets understanding render sheet images for layout context. Every source column
    is kept — scenario is enforced later, in execute, and a budget column can only
    fill a slot that explicitly asks for budget; nothing is dropped by tag here.

    Returns (catalogue, source_kind)."""
    try:
        sheets = understand_source(snapshot, content_hash, source_path=source_path)
        cat = catalogue_from_understanding(snapshot, sheets, as_of=as_of)
        if cat:
            return cat, "ai_understanding"
        logger.warning("source understanding produced 0 series — falling back to deterministic detection")
    except SpendCapExceeded:
        raise
    except Exception:
        logger.exception("source understanding failed — falling back to deterministic detection")
    return build_catalogue(snapshot, source_periods), "deterministic_fallback"


def _reset_preview(version_id: str, facts: list[dict]) -> dict:
    """What a reset would touch, BEFORE any spend: in-scope input cells split by
    what they hold (stale numeric literal vs connector/formula with a cached
    value). Best-effort — empty on any failure."""
    try:
        snap = json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    except Exception:  # noqa: BLE001
        return {}
    cell_info: dict[tuple[str, str], tuple[bool, object]] = {}
    for s in snap.get("sheets", []):
        for c in s.get("cells", []):
            addr = (c.get("address") or "").upper()
            if addr:
                v = c.get("value")
                is_formula = bool(c.get("formula")) or (isinstance(v, str) and v.startswith("="))
                cell_info[(s["name"], addr)] = (is_formula, effective_value(c))
    stale_values = formula_cells = 0
    for f in facts:
        info = cell_info.get((f.get("sheet_name"), (f.get("cell") or "").upper()))
        if info is None:
            continue
        is_formula, ev = info
        if is_formula:
            if ev not in (None, ""):
                formula_cells += 1
        elif isinstance(ev, (int, float)) and not isinstance(ev, bool):
            stale_values += 1
    return {"cells_in_scope": len(facts),
            "stale_values_cleared_by_standard_reset": stale_values,
            "stale_connector_cells_cleared_only_by_full_reset": formula_cells}


def _run_population(target_template_id: str, source_snapshot: dict,
                    source_periods: dict[str, list[dict]], source_label: str,
                    as_of_date: str | None, *, content_hash: str | None = None,
                    source_path: Path | None = None,
                    display_unit: str | None = None, reset: str = "values",
                    add_lines: str = "apply", dry_run: bool = False,
                    deep_rescue: bool = True) -> dict:
    """Core: understand the SOURCE with AI (period columns + data series + units,
    cached by file), build the catalogue from that, ask the LLM to map template
    metrics → source series, then verify+execute the plan (periods/scale-by-magnitude/sign) and read
    the real (cached) values from the snapshot. The template is NOT re-read; we only
    need its workbook to write the values into. Everything is under the spend cap."""
    # Arm the spend firewall for this run (TEMPO_MAX_RUN_USD): source-understanding
    # + mapping. Every LLM call inside checks against it and aborts before breaching.
    set_guard(SpendGuard(default_cap_usd()))
    progress.set_stage(target_template_id, "understanding")

    demand, target_inputs = build_demand(target_template_id, as_of_date)
    # as-of is the pack's reporting vintage / timeline anchor only — it does NOT
    # classify actual vs forecast. Scenario is the source's own column tag, and no
    # source column is ever dropped for lacking an as-of; a time series carries data
    # before and after the as-of alike.
    as_of = parse_any_date(as_of_date)

    if dry_run:
        # Cost-check BEFORE spending: source understanding (free if cached) + mapping.
        cached = cached_sheets(content_hash)
        if cached is not None:
            catalogue = catalogue_from_understanding(source_snapshot, cached, as_of=as_of)
            src_est, src_state = 0.0, "cached"
        else:
            catalogue = {}
            src_est, src_state = estimate_source_understanding_usd(source_snapshot), "would_run"
        ctx = ""
        try:
            preview_vid, _, _ = sb.get_latest_file(target_template_id)
            reset_preview = _reset_preview(preview_vid, target_inputs)
            ctx = load_context(target_template_id, preview_vid)
        except Exception:  # noqa: BLE001 — preview is best-effort
            reset_preview = {}
        return {
            "dry_run": True, "target_template_id": target_template_id,
            "source_filename": source_label,
            "demand_metrics": len(demand["metrics"]),
            "input_cells_to_fill": len(target_inputs),
            "source_understanding": src_state,
            "source_series": len(catalogue),
            "estimated_source_understanding_usd": src_est,
            "estimated_mapping_usd": estimate_mapping_usd(demand["metrics"], catalogue, context=ctx) if catalogue else None,
            "run_cap_usd": default_cap_usd(),
            "reset_preview": reset_preview,
            "context_chars": len(ctx),
        }

    catalogue, catalogue_source = _build_source_catalogue(
        source_snapshot, source_periods, content_hash, source_path, as_of)

    # We only need the template WORKBOOK to write the filled values into — the
    # template's content is already captured in the data model (demand), so the
    # matcher never re-reads it.
    t_vid, t_path, t_fn = sb.get_latest_file(target_template_id)
    try:
        tgt_tmp = _download_to_temp(t_path, t_fn)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Template workbook is missing from storage — re-upload this template.") from e

    # Template cell formats + per-row magnitudes — lets scale be decided by what the
    # template cell actually holds (robust) instead of by unit labels (a mess).
    t_snap = _load_template_snap(t_vid)
    template_context = _template_context(t_snap)

    # The template's OWN check cells (Checks sheets, tie-outs, OK/ERROR flags) —
    # discovered up front so render_filled can recalculate and read them back.
    template_check_cells: list = []
    if t_snap:
        try:
            from app.population.template_checks import collect_check_cells
            from app.understanding.persist import get_understanding
            try:
                t_und = get_understanding(target_template_id)
            except Exception:  # noqa: BLE001 — checks degrade to name/shape discovery
                t_und = None
            template_check_cells = collect_check_cells(t_snap, t_und)
        except Exception as e:  # noqa: BLE001 — verification must never block a fill
            logger.warning("check discovery failed: %s", e)

    # The business-context channel: sponsor notes + answered review questions +
    # strict author rules ride into every mapping batch. Best-effort — an empty
    # context degrades to label/definition matching, never blocks the run.
    biz_context = load_context(target_template_id, t_vid)

    links = notes = None
    coverage_notes: list[str] = []
    reconciled_metrics: list = []
    open_questions: list = []
    unused_source_series: list = []
    routing = {"series": len(catalogue), "catalogue_source": catalogue_source}
    if biz_context:
        routing["context_chars"] = len(biz_context)
    if demand.get("gated_cells"):
        routing["gated_cells"] = demand["gated_cells"]   # role-gated, never silent
    if demand.get("protected_totals"):
        routing["protected_totals"] = demand["protected_totals"]
    filled_url = audit_url = None
    check_results: list = []
    template_checks: dict = {}
    clear_stats: dict = {}
    proposals: list = []
    add_notes: list[str] = []
    additions_applied: list = []
    additions_skipped: list = []
    try:
        # the planner sees the SLOT FACTS (per-sheet grain, date range, emptiness)
        # so grain/rollup/unit judgments rest on reality, not guesses.
        _attach_slot_facts(demand["metrics"], target_inputs, template_context)
        progress.set_stage(target_template_id, "planning",
                           f"{len(demand['metrics'])} metrics")
        metric_maps, mapping_failed = map_metrics(demand["metrics"], catalogue, context=biz_context)
        if mapping_failed:
            # LOUD: name the affected metrics and file a durable review item —
            # a silently-dropped batch once meant up to 80 metrics vanished.
            routing["mapping_failed_metrics"] = mapping_failed[:80]
            try:
                from app.review.items import make_item
                sb.insert_review_items(t_vid, [make_item(
                    source="populate", kind="judgment",
                    question=(f"{len(mapping_failed)} template metric(s) went unmapped because a "
                              "mapping batch failed after retry — re-run populate for these."),
                    why=f"Affected: {', '.join(mapping_failed[:15])}"
                        + ("…" if len(mapping_failed) > 15 else ""),
                )])
            except Exception as e:  # noqa: BLE001
                logger.warning("could not file mapping-failure review item: %s", e)

        # DEEP RESCUE: give each metric the fast batched pass could NOT place its own
        # focused agent (one metric + the whole catalogue), run in parallel. Their
        # deeper opinions overlay the main map for those metrics and then flow through
        # the same verify -> execute -> reconcile/review machinery. Best-effort,
        # spend-capped; a metric that isn't rescued stays exactly as the main pass had it.
        if deep_rescue and catalogue:
            mapped_ok = {m.metric for m in metric_maps if m.series_id}
            weak = [dm for dm in demand["metrics"] if dm["metric"] not in mapped_ok]
            if weak:
                from app.population.rescue import rescue_metrics
                used_series = ({m.series_id for m in metric_maps if m.series_id}
                               | {sid for m in metric_maps for sid in (m.also_series_ids or [])})
                rescued = rescue_metrics(weak, catalogue, used_series=used_series, context=biz_context)
                if rescued:
                    by = {m.metric: m for m in metric_maps}
                    for rm in rescued:
                        by[rm.metric] = rm     # per-metric deep opinion wins for the weak ones
                    metric_maps = list(by.values())
                    routing["rescue_attempted"] = len(weak)
                    routing["rescue_placed"] = sum(1 for rm in rescued if rm.series_id)

        # Double-count guard scope: from the template's OWN formulas, which totals
        # each metric feeds. Reuse of a source series is blocked only when two
        # metrics share a total (would inflate it); a KPI mirrored across sheets
        # feeds no shared total and fills freely. None (no formula graph) => the
        # guard falls back to the conservative global block in verify_plan.
        from app.population.aggregation import metric_totals
        try:
            agg_membership = metric_totals(target_inputs, t_snap)
        except Exception as e:  # noqa: BLE001 — never let graph analysis sink a fill
            logger.warning("aggregation membership failed (%s) — global double-count guard", e)
            agg_membership = None
        if agg_membership is not None:
            routing["totals_modelled"] = len({t for ts in agg_membership.values() for t in ts})

        # ---- FILL-PLAN PATH (docs/Fill-Plan-Architecture.md — the only path) ----
        # plan -> contract overlay -> verify -> one repair round -> execute.
        from app.population.contract import (
            apply_decisions, load_decisions, source_fingerprint)
        from app.population.execute import execute_plan
        from app.population.mapping import repair_plan
        from app.population.verify import blocked_metrics, verify_plan

        plan_issues: list = []
        src_fp = source_fingerprint(source_snapshot)
        decisions = load_decisions(t_vid, fingerprint=src_fp)
        n_dec = apply_decisions(metric_maps, decisions)
        if n_dec:
            routing["contract_decisions_applied"] = n_dec
        progress.set_stage(target_template_id, "verifying")
        issues = verify_plan(metric_maps, catalogue, target_inputs, demand,
                             template_context, agg_membership)
        repairable: dict[str, list] = {}
        for i in issues:
            if i.severity == "repair":
                repairable.setdefault(i.metric, []).append(i)
        if repairable:
            by_fill = {m.metric: m for m in metric_maps}
            metric_by_key = {m["metric"]: m for m in demand["metrics"]}
            from app.population.schema import MetricMap as _MM
            failing = [(metric_by_key.get(mk) or {"metric": mk, "label": mk},
                        by_fill.get(mk) or _MM(metric=mk), iss)
                       for mk, iss in repairable.items()]
            repaired = repair_plan(failing, catalogue, context=biz_context)
            if repaired:
                for rm in repaired:
                    if rm.metric in repairable:
                        by_fill[rm.metric] = rm
                metric_maps = list(by_fill.values())
                routing["plan_repaired"] = len(repaired)
                issues = verify_plan(metric_maps, catalogue, target_inputs, demand,
                                     template_context, agg_membership)
        for i in issues:            # unrepaired -> the question tier, never a dead end
            if i.severity == "repair":
                i.severity = "question"
        blocked = blocked_metrics(issues)
        links, bind_unmatched, exec_issues = execute_plan(
            target_inputs, catalogue, metric_maps, demand,
            display_unit=display_unit, template_context=template_context,
            blocked=blocked)
        plan_issues = issues + exec_issues
        if plan_issues:
            routing["plan_issues"] = dict(Counter(i.code for i in plan_issues))
        # Filled cells that need a human eye: scale that couldn't be magnitude-verified,
        # a reconciled fill, positional alignment, or a flagged auto-resolution.
        review = [{"template_sheet": lk.template_sheet, "template_cell": lk.template_cell, "note": lk.note}
                  for lk in links if lk.note and any(t in lk.note for t in
                                                     ("unverified", "reconciled", "positional", "auto-resolved"))]
        notes = [m.note for m in metric_maps if m.series_id and m.note][:200]
        # WHY the uncovered metrics are uncovered, in the mapper's own words: a null
        # mapping carries a reason ('source combines depreciation & amortisation',
        # 'source splits opex by function, template wants it by nature') so a blank
        # is explained to the user instead of mysterious.
        coverage_notes = [m.note for m in metric_maps if not m.series_id and m.note][:100]
        # Never silently drop source data: any source series no mapping used at all —
        # this is how UNDER-counting (e.g. G&A left out of a reconciled opex line)
        # surfaces instead of hiding. A real-but-unused cost line is a red flag.
        # USED = produced at least one real link. A series claimed by a mapping
        # that never executed (e.g. a derived-math reconcile the executor can't
        # run) is still AVAILABLE — counting it used once masked the entire
        # revenue block sitting empty while 'Total turnover' fed an unexecutable
        # growth metric.
        filled_metric_keys = {metric_key(f) for f in target_inputs
                              if (f.get("sheet_name"), (f.get("cell") or "").upper())
                              in {(lk.template_sheet, (lk.template_cell or "").upper()) for lk in links}}
        used_series = ({m.series_id for m in metric_maps
                        if m.series_id and m.metric in filled_metric_keys}
                       | {sid for m in metric_maps if m.metric in filled_metric_keys
                          for sid in (m.also_series_ids or [])})
        unused_source_series = sorted(s.label for sid, s in catalogue.items() if sid not in used_series)
        # No silent narrowing of COVERAGE either: name the source sheets the
        # densest-N selection never sent to understanding — a sparse but critical
        # sheet must be visible, not invisibly dropped.
        try:
            from app.population.source_understanding import select_sheets
            seen_sheets = {s.get("name") for s in select_sheets(source_snapshot)}
            skipped_src = [s.get("name") for s in source_snapshot.get("sheets", [])
                           if s.get("name") not in seen_sheets]
            if skipped_src:
                routing["source_sheets_skipped"] = skipped_src[:20]
        except Exception:  # noqa: BLE001 — accounting only
            pass
        result = apply_links(target_inputs, source_snapshot, links, skipped=[])

        # Rule enforcement: check every written value against the template's own
        # declared sign convention. Violations stay written (the reviewer decides)
        # but are flagged here AND filed as durable review items in the inbox.
        from app.population.checks import sign_violations, violations_to_review_items
        violations = sign_violations(result.filled, target_inputs)
        for v in violations:
            review.append({"template_sheet": v["template_sheet"], "template_cell": v["template_cell"],
                           "note": f"sign violation: expected {v['expected']} ({v['rule'][:60]})"})
        if violations:
            try:
                sb.insert_review_items(t_vid, violations_to_review_items(violations, source_label))
            except Exception as e:  # noqa: BLE001 — inbox filing must never fail the run
                logger.warning("could not file sign-violation review items: %s", e)

        # Upgrade apply_links' generic "no source match" to the executor's precise reason
        # (low confidence / no source period / unit unresolved / currency mismatch).
        reasons = {(u.get("template_sheet"), (u.get("template_cell") or "").upper()): u.get("reason")
                   for u in bind_unmatched}
        for u in result.unmatched:
            k = (u.get("template_sheet"), (u.get("template_cell") or "").upper())
            if u.get("reason") == "no source match" and k in reasons:
                u["reason"] = reasons[k]

        # Reason histogram: the headline of WHY cells are blank belongs in the
        # response, not buried in a 1,000-row audit list.
        unmatched_reasons = [{"reason": r, "count": n}
                             for r, n in Counter(u.get("reason") for u in result.unmatched).most_common(10)]
        # '298x no source series mapped' reads as failure when most of it is the
        # source honestly not containing those metrics — NAME them so the user
        # can see at a glance what this source doesn't cover.
        label_by_key = {m["metric"]: (m.get("label") or m["metric"]) for m in demand["metrics"]}
        unmapped_metrics = sorted({label_by_key.get(u.get("metric"), str(u.get("metric")))
                                   for u in result.unmatched
                                   if str(u.get("reason", "")).startswith("no source series")})

        # RECONCILIATIONS: metrics the source carries at a different granularity, filled
        # provisionally with an explicit assumption. Surface them, and file each as a
        # durable review question so the user confirms/corrects ONCE — the answer then
        # feeds every future run via the mapping context channel (load_context).
        reconciled_metrics = [{"metric": label_by_key.get(m.metric, m.metric),
                               "assumption": m.assumption or ""}
                              for m in metric_maps if m.status == "reconcile"]
        # needs_decision: the source has related data but assigning it needs a human
        # choice we must not guess — asked (never filled), so the user decides once.
        open_questions = [{"metric": label_by_key.get(m.metric, m.metric),
                           "question": m.assumption or m.note or ""}
                          for m in metric_maps if m.status == "needs_decision"]
        if reconciled_metrics or open_questions:
            try:
                from app.review.items import make_item
                items = []
                if reconciled_metrics:
                    # ONE tap, not N: the reconciles are one judgment call ("fill
                    # close matches rather than leave blanks"); per-metric detail
                    # lives in `why`, granular remaps stay possible on the
                    # contract page.
                    names = [r["metric"] for r in reconciled_metrics]
                    listed = ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
                    items.append(make_item(
                        source="populate", kind="judgment",
                        question=(f"{len(names)} line(s) filled from closest matches "
                                  f"({listed}) — keep them?"),
                        why=chr(10).join(f"{r['metric']}: {r['assumption']}"
                                         for r in reconciled_metrics),
                        affected={"metrics": names},
                        suggested_answer="yes — keep them",
                    ))
                items += [make_item(
                    source="populate", kind="judgment",
                    question=f"{q['metric']}: {(q['question'] or 'needs your mapping decision').rstrip('.')}?",
                    why="The source has related data but the mapping needs your decision; left blank until you choose.",
                    affected={"metrics": [q["metric"]]},
                    suggested_answer="leave it blank",
                ) for q in open_questions]
                sb.insert_review_items(t_vid, items)
            except Exception as e:  # noqa: BLE001 — inbox filing must never fail the run
                logger.warning("could not file reconciliation/decision review items: %s", e)

        # FILL-PLAN questions: every question-tier verifier/executor issue becomes
        # ONE batched review item per (metric, issue) with the plan's proposal as
        # the one-tap answer; the answer persists as a contract decision and
        # replays on every future run. Flagged auto-resolutions land in `review`.
        if plan_issues:
            from app.population.contract import decision_spec
            from app.review.items import make_item
            _DEC_FIELD = {"LOW_CONFIDENCE": "confirmed", "GRAIN_UNBRIDGEABLE": "rollup",
                          "SCALE_CONFLICT": "source_unit", "BUCKET_INCOMPLETE": "rollup"}
            plan_q_items = []
            # SCENARIO_NO_SOURCE is one underlying fact (this source has no
            # budget/forecast data), not N per-metric decisions — ONE question.
            scen_gaps = [i for i in plan_issues if i.code == "SCENARIO_NO_SOURCE"
                         and i.severity == "question"]
            if scen_gaps:
                metrics = sorted({label_by_key.get(i.metric, i.metric) for i in scen_gaps})
                scens = sorted({i.scenario for i in scen_gaps if i.scenario})
                plan_q_items.append(make_item(
                    source="populate-plan", kind="judgment",
                    question=(f"The template has {'/'.join(scens)} columns for "
                              f"{len(metrics)} metric(s) but this source carries no "
                              f"{'/'.join(scens)} data — those slots stay blank. OK?"),
                    why="Affected: " + ", ".join(metrics[:12]) + ("…" if len(metrics) > 12 else ""),
                    suggested_answer="yes — leave them blank",
                ))
            for i in plan_issues:
                if i.code == "SCENARIO_NO_SOURCE":
                    continue
                label = label_by_key.get(i.metric, i.metric)
                if i.severity == "default" and i.resolution:
                    review.append({"template_sheet": (i.cells[0].split("!", 1)[0] if i.cells else None),
                                   "template_cell": (i.cells[0].split("!", 1)[1] if i.cells else None),
                                   "note": f"auto-resolved [{i.code}] '{label}': {i.resolution}"})
                    continue
                if i.severity != "question":
                    continue
                field = _DEC_FIELD.get(i.code)
                # unit answers belong to the SOURCE FAMILY ("this pack is in
                # '000s"), not the template — scoped so an unrelated file never
                # inherits them; everything else is template-scoped.
                scope = "source_format" if field == "source_unit" else "template"
                spec = (decision_spec(i.metric, field, i.suggested_resolution,
                                      scope=scope, fingerprint=src_fp) if field else None)
                why = f"[{i.code}] " + (f"Affects {len(i.cells)} cell(s), e.g. "
                                        f"{', '.join(i.cells[:4])}." if i.cells else "")
                # source_format questions carry the family fingerprint in their
                # item_key source — answering source A's unit question must not
                # suppress the SAME question for source family B.
                item_src = (f"populate-plan:{src_fp[:8]}" if scope == "source_format"
                            else "populate-plan")
                plan_q_items.append(make_item(
                    source=item_src, kind="judgment",
                    question=f"{label}: use the suggested answer?",
                    why=f"{i.detail}. {why}",
                    affected={"metrics": [i.metric], "cells": i.cells[:12]},
                    suggested_answer=i.suggested_resolution, check_spec=spec))
            if plan_q_items:
                try:
                    sb.insert_review_items(t_vid, plan_q_items)
                    routing["plan_questions_filed"] = len(plan_q_items)
                except Exception as e:  # noqa: BLE001 — but a LOST question is surfaced, not swallowed
                    logger.warning("could not file plan questions: %s", e)
                    routing["plan_questions_lost"] = len(plan_q_items)

        # UNDER-COUNTING check as a question, not a dead list: source series no
        # mapping touched. One batched informational item (content-addressed, so
        # answering once suppresses it for good).
        if unused_source_series:
            try:
                from app.review.items import make_item
                names = ", ".join(unused_source_series[:15]) + ("…" if len(unused_source_series) > 15 else "")
                sb.insert_review_items(t_vid, [make_item(
                    source="populate-plan", kind="judgment",
                    question=(f"{len(unused_source_series)} source series went unused "
                              f"({names}) — is that expected?"),
                    suggested_answer="yes — nothing missing",
                    why="Unused source data can mean under-counting (a cost line left out of a reconciled total).",
                )])
            except Exception as e:  # noqa: BLE001
                logger.warning("could not file unused-series item: %s", e)

        # Add-line additions: source series that mapped to NO template metric are
        # routed into the template's extensible regions (the BRIDGE: a region whose
        # hosted metric came back unavailable activates with ranked candidates).
        # Default is "apply": blank-slot lines write immediately (flagged + filed
        # in the inbox); occupied-label overwrites are approval-gated one-tap items,
        # and previously-approved ones replay automatically.
        pending_overwrites: list = []
        if add_lines in ("propose", "apply"):
            try:
                from app.population.region_bridge import (
                    approved_addition_proposals, route_additions)
                regions = sb.list_extensible_regions(t_vid)
                if regions:
                    proposals, add_notes = route_additions(
                        catalogue, metric_maps, regions, target_inputs)
                    proposals.extend(approved_addition_proposals(t_vid))
                else:
                    add_notes = ["no extensible regions stored — re-run Understand (or region detection) first"]
            except Exception as e:  # noqa: BLE001 — additions must never sink the fill
                logger.exception("add-line proposal failed")
                add_notes = [f"add-line proposals unavailable: {e}"]

        def _writable(p: dict) -> bool:
            # blank slots and PLACEHOLDER slots write immediately — a throwaway
            # label ('Custom KPI 1') is the area's designed invitation, and
            # every write is logged + filed as a reversible "keep it?" item.
            # Only REAL labels (editable_label) stay approval-gated.
            return ((p.get("slot_mode") or "blank") in ("blank", "placeholder")
                    or p.get("approved") is True)

        writable_proposals = [p for p in proposals if _writable(p)] if add_lines == "apply" else []
        pending_overwrites = [p for p in proposals if not _writable(p)]

        try:
            # refresh-then-fill on the copy: wipe stale data across ALL in-scope
            # inputs (per the reset mode), write the matches, then the writable
            # additions — so uncovered inputs end up empty, not stale.
            sval: dict = {}
            if writable_proposals:
                for s in source_snapshot.get("sheets", []):
                    for c in s.get("cells", []):
                        a = (c.get("address") or "").upper()
                        if a:
                            sval[(s["name"], a)] = effective_value(c)
            progress.set_stage(target_template_id, "writing")
            filled_bytes, clear_stats, additions_applied, additions_skipped, check_results = render_filled(
                tgt_tmp, result.filled, target_inputs, reset=reset,
                additions=(writable_proposals or None), sval=sval,
                checks=template_check_cells)
            filled_path = sb.upload_filled(t_vid, source_label, filled_bytes)
            filled_url = sb.signed_filled_url(filled_path)
        except Exception as e:  # noqa: BLE001 — render failure shouldn't lose the mapping/report
            logger.warning("filled-workbook render/upload failed: %s", e)

        # The template's own verdict: recalculated check cells. Failures are
        # surfaced in the run AND filed as durable review questions.
        if check_results:
            from app.population.template_checks import checks_to_review_items, summarize_checks
            template_checks = {**summarize_checks(check_results),
                               "items": check_results[:100],
                               "calc_seconds": clear_stats.get("calc_seconds")}
            failed_checks = [r for r in check_results if r.get("status") == "fail"]
            for r in failed_checks[:50]:
                review.append({"template_sheet": r["sheet"], "template_cell": r["cell"],
                               "note": f"template check FAILED: {r['label']} "
                                       f"({str(r.get('before'))[:24]} → {str(r.get('after'))[:24]})"})
            if failed_checks:
                try:
                    sb.insert_review_items(t_vid, checks_to_review_items(failed_checks, source_label))
                except Exception as e:  # noqa: BLE001 — inbox filing must never fail the run
                    logger.warning("could not file template-check review items: %s", e)

        # Additions into the inbox: written lines as informational "keep it?" items,
        # occupied-label proposals as one-tap approvals (approving replays next run).
        if additions_applied or pending_overwrites:
            try:
                from app.population.region_bridge import addition_review_items
                sb.insert_review_items(
                    t_vid, addition_review_items(additions_applied, pending_overwrites, source_label))
            except Exception as e:  # noqa: BLE001 — inbox filing must never fail the run
                logger.warning("could not file addition review items: %s", e)

        # Persist the full audit (demand, routing, every link, skipped, unmatched).
        try:
            audit = {
                "target_template_id": target_template_id, "source_filename": source_label,
                "as_of_date": as_of_date, "demand": demand, "routing": routing,
                "links": [lk.model_dump(mode="json") for lk in links],
                "filled": [fc.model_dump(mode="json") for fc in result.filled],
                "unmatched": result.unmatched, "unmatched_reasons": unmatched_reasons,
                "skipped": result.skipped,
                "review": review, "notes": notes, "coverage_notes": coverage_notes,
                "reconciled": reconciled_metrics, "unused_source_series": unused_source_series,
                "template_checks": template_checks,
                "summary": result.summary,
                "rule_violations": violations, "context_chars": len(biz_context),
                "fill_plan": [m.model_dump(exclude_none=True) for m in metric_maps],
                "plan_issues": [i.model_dump(exclude_none=True) for i in plan_issues],
                "reset": reset, **clear_stats,
                "proposed_additions": proposals, "addition_notes": add_notes,
                "additions_applied": additions_applied, "additions_skipped": additions_skipped,
            }
            audit_path = sb.upload_audit(t_vid, source_label, json.dumps(audit, default=str).encode())
            audit_url = sb.signed_filled_url(audit_path)
        except Exception as e:  # noqa: BLE001 — audit is best-effort
            logger.warning("audit upload failed: %s", e)
    finally:
        tgt_tmp.unlink(missing_ok=True)

    filled = [fc.model_dump(mode="json") for fc in result.filled]
    return {
        "target_template_id": target_template_id,
        "source_filename": source_label,
        "as_of_date": as_of_date,
        "demand_metrics": len(demand["metrics"]),
        "summary": result.summary,
        "routing": routing,
        "links_count": len(links),
        "filled": filled[:500],
        "filled_truncated": len(filled) > 500,
        "unmatched": result.unmatched[:200],
        "unmatched_truncated": len(result.unmatched) > 200,
        "unmatched_count": len(result.unmatched),
        "unmatched_reasons": unmatched_reasons,
        "unmapped_metrics": unmapped_metrics[:60],
        "skipped": result.skipped[:200],
        "skipped_truncated": len(result.skipped) > 200,
        "skipped_count": len(result.skipped),
        "review": review[:200],
        "review_truncated": len(review) > 200,
        "review_count": len(review),
        "coverage_notes": coverage_notes,
        "reconciled": reconciled_metrics,
        "reconciled_count": len(reconciled_metrics),
        "open_questions": open_questions,
        "open_questions_count": len(open_questions),
        "unused_source_series": unused_source_series,
        "template_checks": template_checks,
        "rule_violations": violations[:100],
        "rule_violation_count": len(violations),
        "reset": reset,
        "cleared_count": clear_stats.get("cleared_values", 0) + clear_stats.get("cleared_formulas", 0),
        "cleared_values": clear_stats.get("cleared_values", 0),
        "cleared_formulas": clear_stats.get("cleared_formulas", 0),
        "proposed_additions": proposals[:100],
        "pending_label_overwrites": pending_overwrites[:50],
        "addition_notes": add_notes[:20],
        "additions_applied": additions_applied[:100],
        "additions_skipped": additions_skipped[:50],
        "notes": notes,
        "fill_plan": [m.model_dump(exclude_none=True) for m in metric_maps],
        "plan_issues": [i.model_dump(exclude_none=True) for i in plan_issues][:100],
        "filled_url": filled_url,
        "audit_url": audit_url,
    }


def _detect_source_periods(parsed) -> dict[str, list[dict]]:
    """Per-sheet period columns from deterministic detection — real dates the
    executor aligns template slots against. {sheet: [{col, parsed_date, period_type}]}."""
    out: dict[str, list[dict]] = {}
    for p in detect_structure(parsed).periods:
        out.setdefault(p.sheet_name, []).append(
            {"col": p.col, "parsed_date": p.parsed_date, "period_type": p.period_type})
    return out


def populate_from_snapshot(target_template_id: str, source_filename: str, snapshot: dict,
                           as_of_date: str | None = None, *, display_unit: str | None = None,
                           reset: str = "values", add_lines: str = "apply",
                           dry_run: bool = False, deep_rescue: bool = True) -> dict:
    """Populate from a CLIENT-SERIALIZED workbook snapshot — the Excel add-in
    path: the user's open workbook is read in place via Office.js (values,
    formulas, number formats) and posted as JSON; no file ever leaves Excel.
    Reconstructing a ParsedWorkbook from the snapshot reuses the exact same
    deterministic period detection as the upload path."""
    from app.reconstruct import reconstruct_workbook_from_snapshot

    parsed = reconstruct_workbook_from_snapshot(snapshot)
    source_periods = _detect_source_periods(parsed)
    ch = source_cache.content_hash(
        json.dumps(snapshot, sort_keys=True, default=str).encode())
    return _run_population(target_template_id, snapshot, source_periods,
                           source_filename or "workbook", as_of_date,
                           content_hash=ch, source_path=None,
                           display_unit=display_unit, reset=reset,
                           add_lines=add_lines, dry_run=dry_run, deep_rescue=deep_rescue)


def populate_from_bytes(target_template_id: str, source_filename: str, source_bytes: bytes,
                        as_of_date: str | None = None, *, display_unit: str | None = None,
                        reset: str = "values", add_lines: str = "apply",
                        dry_run: bool = False, deep_rescue: bool = True) -> dict:
    """Populate a template directly from an uploaded data file's bytes. Parses
    the source in-memory (Aspose → snapshot) — it is never stored as a template.
    This is the drag-a-file-onto-a-template path.

    Pass dry_run=True to get a cost estimate without any LLM call. display_unit
    lets the consultant declare the output basis (e.g. 'EUR millions') so scale
    resolves deterministically when the template carries no unit signals."""
    src_tmp = _bytes_to_temp(source_filename, source_bytes)
    try:
        parsed = parse_workbook(src_tmp)
        snapshot = workbook_to_snapshot(parsed)
        source_periods = _detect_source_periods(parsed)   # deterministic fallback only
        return _run_population(target_template_id, snapshot, source_periods,
                               source_filename or "source.xlsx", as_of_date,
                               content_hash=source_cache.content_hash(source_bytes),
                               source_path=src_tmp,   # alive until the run returns → images
                               display_unit=display_unit, reset=reset,
                               add_lines=add_lines, dry_run=dry_run, deep_rescue=deep_rescue)
    finally:
        src_tmp.unlink(missing_ok=True)
