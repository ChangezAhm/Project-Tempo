"""Population orchestrator (Build A): parse-source is reused upload+parse, then
match (LLM) → apply (deterministic) → render filled workbook + attribution.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from app import supabase_client as sb
from app.datamodel.derive import DERIVATION_VERSION
from app.datamodel.persist import derive_and_persist, get_data_model
from app.population.apply import apply_links
from app.population.match import build_links
from app.raw_extraction.workbook_parser import parse_workbook
from app.snapshot import workbook_to_snapshot

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


def _run_population(target_template_id: str, source_snapshot: dict, source_path: Path,
                    source_label: str, as_of_date: str | None) -> dict:
    """Core: locate the target's already-analysed inputs in the parsed source (the
    AI reads ONLY the source, driven by the saved analysis), read values
    deterministically, render + upload the filled workbook + a JSON audit. The
    template is NOT re-read; we only need its workbook to write the values into."""
    demand, target_inputs = build_demand(target_template_id, as_of_date)

    # We only need the template WORKBOOK to write the filled values into — the
    # template's content is already captured in the data model (demand), so the
    # matcher never re-reads it.
    t_vid, t_path, t_fn = sb.get_latest_file(target_template_id)
    try:
        tgt_tmp = _download_to_temp(t_path, t_fn)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Template workbook is missing from storage — re-upload this template.") from e

    links = skipped = routing = notes = None
    filled_url = audit_url = None
    cleared = 0
    try:
        links, skipped, routing, notes = build_links(target_inputs, source_snapshot, source_path, demand)
        result = apply_links(target_inputs, source_snapshot, links, skipped)

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
                "notes": notes, "summary": result.summary, "cleared_count": cleared,
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
        "cleared_count": cleared,
        "notes": notes,
        "filled_url": filled_url,
        "audit_url": audit_url,
    }


def populate_from_bytes(target_template_id: str, source_filename: str, source_bytes: bytes,
                        as_of_date: str | None = None) -> dict:
    """Populate a template directly from an uploaded data file's bytes. Parses
    the source in-memory (Aspose → snapshot) — it is never stored as a template.
    This is the drag-a-file-onto-a-template path."""
    src_tmp = _bytes_to_temp(source_filename, source_bytes)
    try:
        parsed = parse_workbook(src_tmp)
        snapshot = workbook_to_snapshot(parsed)
        return _run_population(target_template_id, snapshot, src_tmp,
                               source_filename or "source.xlsx", as_of_date)
    finally:
        src_tmp.unlink(missing_ok=True)
