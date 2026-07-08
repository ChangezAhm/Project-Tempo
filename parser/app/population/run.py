"""Population orchestrator (Build A): parse-source is reused upload+parse, then
match (LLM) → apply (deterministic) → render filled workbook + attribution.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from app import supabase_client as sb
from app.datamodel.derive import DERIVATION_VERSION
from app.datamodel.persist import derive_and_persist, get_data_model
from app.population import source_cache
from app.population.apply import apply_links
from app.population.binding import bind
from app.population.catalogue import build_catalogue, catalogue_from_understanding, effective_value
from app.population.periods import parse_any_date
from app.population.cost import SpendCapExceeded, SpendGuard, default_cap_usd, set_guard
from app.population.mapping import estimate_mapping_usd, map_metrics
from app.population.source_understanding import (
    cached_sheets, estimate_source_understanding_usd, understand_source,
)
from app.raw_extraction.workbook_parser import parse_workbook
from app.snapshot import workbook_to_snapshot
from app.structure.detect import detect_structure

logger = logging.getLogger(__name__)


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
    inputs = [f for f in dm["facts"] if f.get("category") in ("data", "sourced")]
    metrics: dict[str, dict] = {}
    for f in inputs:
        key = f.get("canonical_metric") or f.get("metric_label")
        if key and key not in metrics:
            metrics[key] = {"metric": key, "label": f.get("metric_label"), "unit": f.get("unit")}
    period_count = max((f["period_index"] for f in inputs if f.get("period_index") is not None), default=-1) + 1
    scenarios = sorted({f["scenario"] for f in inputs if f.get("scenario") and f["scenario"] != "unknown"})
    grains = (dm["model"] or {}).get("period_grains") or ["monthly"]
    demand = {"as_of_date": as_of_date, "period_count": period_count,
              "period_grain": grains[0] if grains else "monthly",
              "scenarios": scenarios, "metrics": list(metrics.values())}
    return demand, inputs


def _is_clearable_value(value, is_formula: bool) -> bool:
    """Stale data to wipe on refresh = a plain NUMBER sitting in an input cell.
    Never clear a formula (computed/connector cell) or text (a label/header) — only
    numeric literals, so structure and computed cells are untouched."""
    if is_formula:
        return False
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def render_filled(template_workbook_path, filled, clear_facts=()) -> tuple[bytes, int]:
    """Refresh-then-fill: first wipe stale numeric values in the in-scope input
    cells (``clear_facts`` = the template's data/sourced facts) so a previously
    populated template doesn't leak another company's numbers, then write the
    matched values. Formulas and text are never cleared. Returns (bytes, cleared)."""
    from aspose.cells import Workbook
    wb = Workbook(str(template_workbook_path))
    ws_by_name = {w.name: w for w in wb.worksheets}

    cleared = 0
    for f in clear_facts:
        ws = ws_by_name.get(f.get("sheet_name"))
        if ws is None or not f.get("cell"):
            continue
        cell = ws.cells.get(f["cell"])
        if _is_clearable_value(cell.value, cell.is_formula):
            ws.cells.clear_contents(cell.row, cell.column, cell.row, cell.column)
            cleared += 1

    for fc in filled:
        ws = ws_by_name.get(fc.template_sheet)
        if ws is not None:
            ws.cells.get(fc.template_cell).put_value(fc.value)

    fd, name = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    out = Path(name)
    try:
        wb.save(str(out))
        return out.read_bytes(), cleared
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


def _template_context(version_id: str) -> tuple[dict, dict, dict, set]:
    """From the template snapshot, the maps binding needs:
      - numfmt[(sheet, A1)]      -> number format (kind/currency for scale)
      - mags[(sheet, row)]       -> numeric magnitudes already in the row (scale by
                                    what the cell holds, not by its unit label)
      - dates_by_col[(sheet,col)]-> the column's real period date, read from the
                                    sheet's timeline header row (the row carrying the
                                    most dates), so periods align by actual date.
      - formula_cells{(sheet,A1)}-> cells that are formulas (computed subtotals/totals);
                                    we never write into these — they recompute themselves.
    Best-effort — empty on any failure (binding then uses label scale + positional)."""
    numfmt: dict[tuple[str, str], str] = {}
    mags: dict[tuple[str, int], list[float]] = defaultdict(list)
    dates_by_col: dict[tuple[str, int], object] = {}
    formula_cells: set[tuple[str, str]] = set()
    try:
        snap = json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    except Exception as e:  # noqa: BLE001
        logger.info("template snapshot unavailable for scale/date context (%s)", e)
        return {}, {}, {}, set()
    for s in snap.get("sheets", []):
        name = s.get("name")
        row_dates: dict[int, dict[int, object]] = defaultdict(dict)   # row -> {col: date}
        for c in s.get("cells", []):
            addr = (c.get("address") or "").upper()
            nf = (c.get("style") or {}).get("number_format")
            if addr and nf:
                numfmt[(name, addr)] = nf
            v = c.get("value")
            if addr and (c.get("cell_type") == "formula" or c.get("formula")
                         or (isinstance(v, str) and v.startswith("="))):
                formula_cells.add((name, addr))
            ev = effective_value(c)
            if isinstance(ev, (int, float)) and not isinstance(ev, bool) and ev:
                mags[(name, c.get("row"))].append(float(ev))
            d = parse_any_date(ev)
            if d is not None:
                row_dates[c["row"]][c["col"]] = d
        # the timeline = the row with the most dates; map its columns to dates
        if row_dates:
            timeline = max(row_dates.values(), key=len)
            for col, d in timeline.items():
                dates_by_col[(name, col)] = d
    return numfmt, dict(mags), dates_by_col, formula_cells


def _build_source_catalogue(snapshot: dict, source_periods: dict, content_hash: str | None,
                            source_path: Path | None = None):
    """Catalogue the source via AI understanding (robust to PortCo layout variance),
    falling back to deterministic detection if understanding yields nothing. A spend
    cap breach is never swallowed. ``source_path`` (the uploaded workbook on disk)
    lets understanding render sheet images for layout context."""
    try:
        sheets = understand_source(snapshot, content_hash, source_path=source_path)
        cat = catalogue_from_understanding(snapshot, sheets)
        if cat:
            return cat, "ai_understanding"
        logger.warning("source understanding produced 0 series — falling back to deterministic detection")
    except SpendCapExceeded:
        raise
    except Exception:
        logger.exception("source understanding failed — falling back to deterministic detection")
    return build_catalogue(snapshot, source_periods), "deterministic_fallback"


def _run_population(target_template_id: str, source_snapshot: dict,
                    source_periods: dict[str, list[dict]], source_label: str,
                    as_of_date: str | None, *, content_hash: str | None = None,
                    source_path: Path | None = None,
                    display_unit: str | None = None, target_currency: str | None = None,
                    fx_rate: float | None = None, dry_run: bool = False) -> dict:
    """Core: understand the SOURCE with AI (period columns + data series + units,
    cached by file), build the catalogue from that, ask the LLM to map template
    metrics → source series, then bind periods/scale(by magnitude)/sign/FX and read
    the real (cached) values from the snapshot. The template is NOT re-read; we only
    need its workbook to write the values into. Everything is under the spend cap."""
    # Arm the spend firewall for this run (TEMPO_MAX_RUN_USD): source-understanding
    # + mapping. Every LLM call inside checks against it and aborts before breaching.
    set_guard(SpendGuard(default_cap_usd()))

    demand, target_inputs = build_demand(target_template_id, as_of_date)

    if dry_run:
        # Cost-check BEFORE spending: source understanding (free if cached) + mapping.
        cached = cached_sheets(content_hash)
        if cached is not None:
            catalogue = catalogue_from_understanding(source_snapshot, cached)
            src_est, src_state = 0.0, "cached"
        else:
            catalogue = {}
            src_est, src_state = estimate_source_understanding_usd(source_snapshot), "would_run"
        return {
            "dry_run": True, "target_template_id": target_template_id,
            "source_filename": source_label,
            "demand_metrics": len(demand["metrics"]),
            "input_cells_to_fill": len(target_inputs),
            "source_understanding": src_state,
            "source_series": len(catalogue),
            "estimated_source_understanding_usd": src_est,
            "estimated_mapping_usd": estimate_mapping_usd(demand["metrics"], catalogue) if catalogue else None,
            "run_cap_usd": default_cap_usd(),
        }

    catalogue, catalogue_source = _build_source_catalogue(source_snapshot, source_periods,
                                                          content_hash, source_path)

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
    template_context = _template_context(t_vid)

    links = notes = None
    routing = {"series": len(catalogue), "catalogue_source": catalogue_source}
    filled_url = audit_url = None
    cleared = 0
    try:
        metric_maps = map_metrics(demand["metrics"], catalogue)
        links, bind_unmatched = bind(
            target_inputs, catalogue, metric_maps, demand,
            target_currency=target_currency, fx_rate=fx_rate, display_unit=display_unit,
            template_context=template_context,
        )
        # Filled cells whose scale couldn't be magnitude-verified — written, but
        # surfaced so a human checks them rather than trusting a label-only scale.
        review = [{"template_sheet": lk.template_sheet, "template_cell": lk.template_cell, "note": lk.note}
                  for lk in links if lk.note and "unverified" in lk.note]
        notes = [m.note for m in metric_maps if m.series_id and m.note][:200]
        result = apply_links(target_inputs, source_snapshot, links, skipped=[])

        # Upgrade apply_links' generic "no source match" to binding's precise reason
        # (low confidence / no source period / unit unresolved / currency mismatch).
        reasons = {(u.get("template_sheet"), (u.get("template_cell") or "").upper()): u.get("reason")
                   for u in bind_unmatched}
        for u in result.unmatched:
            k = (u.get("template_sheet"), (u.get("template_cell") or "").upper())
            if u.get("reason") == "no source match" and k in reasons:
                u["reason"] = reasons[k]

        try:
            # refresh-then-fill: wipe stale numeric values across ALL in-scope inputs,
            # then write the matches — so uncovered inputs end up empty, not stale.
            filled_bytes, cleared = render_filled(tgt_tmp, result.filled, target_inputs)
            filled_path = sb.upload_filled(t_vid, source_label, filled_bytes)
            filled_url = sb.signed_filled_url(filled_path)
        except Exception as e:  # noqa: BLE001 — render failure shouldn't lose the mapping/report
            logger.warning("filled-workbook render/upload failed: %s", e)

        # Persist the full audit (demand, routing, every link, skipped, unmatched).
        try:
            audit = {
                "target_template_id": target_template_id, "source_filename": source_label,
                "as_of_date": as_of_date, "demand": demand, "routing": routing,
                "links": [lk.model_dump(mode="json") for lk in links],
                "filled": [fc.model_dump(mode="json") for fc in result.filled],
                "unmatched": result.unmatched, "skipped": result.skipped,
                "review": review, "notes": notes, "summary": result.summary,
                "cleared_count": cleared,
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
        "unmatched_count": len(result.unmatched),
        "skipped": result.skipped[:200],
        "skipped_count": len(result.skipped),
        "review": review[:200],
        "review_count": len(review),
        "cleared_count": cleared,
        "notes": notes,
        "filled_url": filled_url,
        "audit_url": audit_url,
    }


def _detect_source_periods(parsed) -> dict[str, list[dict]]:
    """Per-sheet period columns from deterministic detection — real dates the
    binder aligns template slots against. {sheet: [{col, parsed_date, period_type}]}."""
    out: dict[str, list[dict]] = {}
    for p in detect_structure(parsed).periods:
        out.setdefault(p.sheet_name, []).append(
            {"col": p.col, "parsed_date": p.parsed_date, "period_type": p.period_type})
    return out


def populate_from_bytes(target_template_id: str, source_filename: str, source_bytes: bytes,
                        as_of_date: str | None = None, *, display_unit: str | None = None,
                        target_currency: str | None = None, fx_rate: float | None = None,
                        dry_run: bool = False) -> dict:
    """Populate a template directly from an uploaded data file's bytes. Parses
    the source in-memory (Aspose → snapshot) — it is never stored as a template.
    This is the drag-a-file-onto-a-template path.

    Pass dry_run=True to get a cost estimate without any LLM call. display_unit /
    target_currency / fx_rate let the consultant declare the output basis (e.g.
    'EUR millions' + a rate) so scale and currency resolve deterministically."""
    src_tmp = _bytes_to_temp(source_filename, source_bytes)
    try:
        parsed = parse_workbook(src_tmp)
        snapshot = workbook_to_snapshot(parsed)
        source_periods = _detect_source_periods(parsed)   # deterministic fallback only
        return _run_population(target_template_id, snapshot, source_periods,
                               source_filename or "source.xlsx", as_of_date,
                               content_hash=source_cache.content_hash(source_bytes),
                               source_path=src_tmp,   # alive until the run returns → images
                               display_unit=display_unit, target_currency=target_currency,
                               fx_rate=fx_rate, dry_run=dry_run)
    finally:
        src_tmp.unlink(missing_ok=True)
