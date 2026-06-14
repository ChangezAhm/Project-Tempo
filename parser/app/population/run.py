"""Population orchestrator (Build A): parse-source is reused upload+parse, then
match (LLM) → apply (deterministic) → render filled workbook + attribution.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
from pathlib import Path

from app import supabase_client as sb
from app.datamodel.persist import get_data_model
from app.population.apply import apply_mapping
from app.population.match import build_mapping

logger = logging.getLogger(__name__)


def build_demand(template_id: str, as_of_date: str | None) -> tuple[dict, list[dict]]:
    """The template's 'demand' (distinct input metrics + period/scenario shape) +
    the input facts to fill. Targets the data/sourced cells (the actual inputs),
    not computed/config/exclude."""
    dm = get_data_model(template_id, limit=30000)
    if not dm.get("available"):
        raise RuntimeError("No data model — derive it first (/datamodel).")
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


def render_filled(template_workbook_path, filled) -> bytes:
    """Write the filled values into the template workbook and return the bytes."""
    from aspose.cells import Workbook
    wb = Workbook(str(template_workbook_path))
    ws_by_name = {w.name: w for w in wb.worksheets}
    for fc in filled:
        ws = ws_by_name.get(fc.template_sheet)
        if ws is not None:
            ws.cells.get(fc.template_cell).put_value(fc.value)
    fd, name = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    out = Path(name)
    try:
        wb.save(str(out))
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


def _download_to_temp(storage_path: str, filename: str) -> Path:
    data = sb.download_workbook(storage_path)
    fd, name = tempfile.mkstemp(suffix=Path(filename).suffix or ".xlsx")
    os.close(fd)
    p = Path(name)
    p.write_bytes(data)
    return p


def populate(target_template_id: str, source_template_id: str, as_of_date: str | None = None) -> dict:
    """First-run population: LLM maps the source to the template's inputs, then
    deterministic apply fills the cells. Returns the mapping + attribution + the
    filled workbook (uploaded as a snippet-style artifact)."""
    demand, target_inputs = build_demand(target_template_id, as_of_date)

    s_vid, s_path, s_fn = sb.get_latest_file(source_template_id)
    source_snapshot = json.loads(gzip.decompress(sb.download_snapshot(s_vid)))
    src_tmp = _download_to_temp(s_path, s_fn)
    try:
        mapping = build_mapping(demand, source_snapshot, src_tmp)
    finally:
        src_tmp.unlink(missing_ok=True)

    result = apply_mapping(target_inputs, source_snapshot, mapping)

    # render the filled workbook + upload for download
    t_vid, t_path, t_fn = sb.get_latest_file(target_template_id)
    tgt_tmp = _download_to_temp(t_path, t_fn)
    filled_url = None
    try:
        filled_bytes = render_filled(tgt_tmp, result.filled)
        filled_path = sb.upload_filled(t_vid, source_template_id, filled_bytes)
        filled_url = sb.signed_filled_url(filled_path)
    except Exception as e:  # noqa: BLE001 — rendering failure shouldn't lose the mapping/report
        logger.warning("filled-workbook render/upload failed: %s", e)
    finally:
        tgt_tmp.unlink(missing_ok=True)

    filled = [fc.model_dump(mode="json") for fc in result.filled]
    return {
        "target_template_id": target_template_id,
        "source_template_id": source_template_id,
        "as_of_date": as_of_date,
        "demand_metrics": len(demand["metrics"]),
        "summary": result.summary,
        "mapping": mapping.model_dump(mode="json"),
        "filled": filled[:500],
        "filled_truncated": len(filled) > 500,
        "unmatched_count": len(result.unmatched),
        "filled_url": filled_url,
    }
