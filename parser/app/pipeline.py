"""Parse-and-persist pipeline (Build Step 2-3).

Stateless: given a template_id, pull the stored workbook from Supabase
Storage, run the Aspose extraction, and write template_sheets back. The
Next.js app owns upload/storage; this service never touches the filesystem
beyond a short-lived temp copy Aspose needs to open.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

from app import supabase_client as sb
from app.labels import derive_column_headers, derive_row_labels
from app.raw_extraction.formula_mapper import _range_bounds, _split_qualified
from app.raw_extraction.workbook_parser import ParsedSheet, parse_workbook
from app.reconstruct import reconstruct_workbook_from_snapshot
from app.snapshot import SNAPSHOT_SCHEMA_VERSION, workbook_to_snapshot
from app.structure.detect import detect_structure
from app.structure.schema import StructureResult

logger = logging.getLogger(__name__)


class SheetNotFound(Exception):
    """Requested sheet name doesn't exist in the workbook."""


def _parse_bytes(filename: str, data: bytes):
    """Write workbook bytes to a short-lived temp file and parse it."""
    suffix = Path(filename).suffix or ".xlsx"
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(name)
    try:
        tmp_path.write_bytes(data)
        return parse_workbook(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _sheet_row(version_id: str, s: ParsedSheet) -> dict:
    formula_count = sum(1 for c in s.cells if c.formula)
    return {
        "template_version_id": version_id,
        "name": s.name,
        "index": s.index,
        "is_hidden": s.is_hidden,
        "is_protected": s.is_protected,
        "tab_color": s.tab_color,
        "used_max_row": s.used_max_row,
        "used_max_col": s.used_max_col,
        "frozen_rows": s.frozen_rows,
        "frozen_cols": s.frozen_cols,
        "print_area": s.print_area,
        "was_truncated": s.was_truncated,
        "cell_count": len(s.cells),
        "formula_count": formula_count,
        "row_labels": derive_row_labels(s.cells),
        "column_headers": derive_column_headers(s.cells),
    }


def parse_and_persist(template_id: str) -> dict:
    """Run structural extraction for a template and persist template_sheets.

    Returns a summary dict. Raises TemplateNotFound if no stored file exists.
    """
    version_id, storage_path, filename = sb.get_latest_file(template_id)
    job_id = sb.create_job(version_id, "parse_structure")

    try:
        data = sb.download_workbook(storage_path)
        logger.info("Parsing %s (%d bytes) for template %s", filename, len(data), template_id)
        parsed = _parse_bytes(filename, data)

        rows = [_sheet_row(version_id, s) for s in parsed.sheets]
        sb.replace_sheets(version_id, rows)

        # Option B: persist the FULL extraction as a gzipped JSON snapshot.
        snapshot = workbook_to_snapshot(parsed)
        blob = gzip.compress(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
        snapshot_path = sb.upload_snapshot(version_id, blob)
        logger.info("Stored snapshot %s (%d gzip bytes)", snapshot_path, len(blob))

        # Layer 2: deterministic structure detection + persistence. Additive —
        # never let it break the core extraction/snapshot (e.g. if the 0003
        # tables aren't migrated yet).
        try:
            structure = detect_structure(parsed)
            struct_counts = persist_structure(version_id, structure)
            logger.info("Persisted structure: %s", struct_counts)
        except Exception as e:  # noqa: BLE001
            logger.warning("Structure detection skipped: %s", e)
            struct_counts = {"error": str(e)[:200]}

        summary = {
            "filename": filename,
            "sheet_count": parsed.metadata.sheet_count,
            "hidden_sheet_count": parsed.metadata.hidden_sheet_count,
            "total_cells": parsed.metadata.total_cells,
            "total_formulas": parsed.metadata.total_formulas,
            "total_named_ranges": parsed.metadata.total_named_ranges,
            "has_vba": parsed.metadata.has_vba,
            "snapshot": {
                "bucket": sb.SNAPSHOT_BUCKET,
                "storage_path": snapshot_path,
                "gzip_bytes": len(blob),
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
            },
            "structure": struct_counts,
            "sheets": [
                {
                    "name": s.name,
                    "index": s.index,
                    "is_hidden": s.is_hidden,
                    "is_protected": s.is_protected,
                    "cell_count": len(s.cells),
                    "used_range": f"{s.used_max_row}r x {s.used_max_col}c",
                }
                for s in parsed.sheets
            ],
        }
        sb.complete_job(job_id, summary)
        logger.info("Persisted %d sheets for template %s", len(rows), template_id)
        return {"job_id": job_id, "template_version_id": version_id, **summary}

    except Exception as e:
        logger.exception("Parse failed for template %s", template_id)
        sb.fail_job(job_id, str(e))
        raise


def _cell_dict(c) -> dict:
    return {
        "address": c.address,
        "type": c.cell_type.value,
        "role": c.role.value,
        "value": c.value,
        "formula": c.formula,
        "bold": c.style.bold,
        "locked": c.style.is_locked,
        "indent": c.style.indent_level,
        "fill": c.style.fill_color,
        "number_format": c.style.number_format,
        "precedents": c.precedents,
    }


def inspect(
    template_id: str,
    sheet: str | None = None,
    limit: int = 200,
    formulas_only: bool = False,
) -> dict:
    """Read-only snapshot of the full in-memory extraction (Build Step 10).

    No ``sheet`` → a workbook overview (metadata, named ranges, formula-graph
    counts, per-sheet stats). With ``sheet`` → that sheet's actual cells
    (capped at ``limit``) plus its validations, text boxes, comments, etc.
    Nothing is persisted — this re-parses on demand purely for inspection.
    """
    version_id, storage_path, filename = sb.get_latest_file(template_id)
    data = sb.download_workbook(storage_path)
    parsed = _parse_bytes(filename, data)

    if sheet is None:
        return {
            "filename": filename,
            "template_version_id": version_id,
            "metadata": parsed.metadata.model_dump(mode="json"),
            "named_ranges": [nr.model_dump() for nr in parsed.named_ranges],
            "formula_graph": {
                "links": len(parsed.formula_graph.links),
                "input_cells": len(parsed.formula_graph.input_cells),
                "output_cells": len(parsed.formula_graph.output_cells),
            },
            "sheets": [
                {
                    "name": s.name,
                    "index": s.index,
                    "is_hidden": s.is_hidden,
                    "is_protected": s.is_protected,
                    "cell_count": len(s.cells),
                    "formula_count": sum(1 for c in s.cells if c.formula),
                    "merged_ranges": len(s.merged_ranges),
                    "data_validations": len(s.data_validations),
                    "text_boxes": len(s.text_box_notes),
                    "comments": len(s.comments),
                    "hyperlinks": len(s.hyperlinks),
                }
                for s in parsed.sheets
            ],
        }

    ps = parsed.get_sheet(sheet)
    if ps is None:
        raise SheetNotFound(
            f"Sheet '{sheet}' not found. Available: {[s.name for s in parsed.sheets]}"
        )

    cells = [c for c in ps.cells if c.formula] if formulas_only else ps.cells
    total = len(cells)
    return {
        "sheet": ps.name,
        "filename": filename,
        "is_hidden": ps.is_hidden,
        "is_protected": ps.is_protected,
        "used_range": f"{ps.used_max_row}r x {ps.used_max_col}c",
        "cell_count": len(ps.cells),
        "formula_count": sum(1 for c in ps.cells if c.formula),
        "returned": min(total, limit),
        "total_matching": total,
        "cells": [_cell_dict(c) for c in cells[:limit]],
        "merged_ranges": [m.model_dump() for m in ps.merged_ranges],
        "data_validations": [v.model_dump() for v in ps.data_validations],
        "text_boxes": [t.model_dump() for t in ps.text_box_notes],
        "comments": [c.model_dump() for c in ps.comments],
        "hyperlinks": [h.model_dump() for h in ps.hyperlinks],
    }


def load_snapshot(
    template_id: str,
    sheet: str | None = None,
    limit: int = 200,
    formulas_only: bool = False,
) -> dict:
    """Read the persisted snapshot from Storage — NO re-parse (Option B).

    Proves the full extraction survives durably: this serves cell/validation/
    text-box data straight out of the stored blob. ``sheet=None`` → overview;
    with ``sheet`` → that sheet's stored cells (capped at ``limit``).
    """
    version_id, _, _ = sb.get_latest_file(template_id)
    blob = sb.download_snapshot(version_id)  # raises if no snapshot yet
    snap = json.loads(gzip.decompress(blob))

    if sheet is None:
        return {
            "source": "stored snapshot (no re-parse)",
            "schema_version": snap["schema_version"],
            "metadata": snap["metadata"],
            "named_ranges_count": len(snap["named_ranges"]),
            "formula_graph": {
                "input_cells": len(snap["formula_graph"]["input_cells"]),
                "output_cells": len(snap["formula_graph"]["output_cells"]),
                "link_count": snap["formula_graph"]["link_count"],
            },
            "sheets": [
                {
                    "name": s["name"],
                    "index": s["index"],
                    "is_hidden": s["is_hidden"],
                    "is_protected": s["is_protected"],
                    "cell_count": len(s["cells"]),
                    "data_validations": len(s["data_validations"]),
                    "text_boxes": len(s["text_box_notes"]),
                }
                for s in snap["sheets"]
            ],
        }

    target = next((s for s in snap["sheets"] if s["name"] == sheet), None)
    if target is None:
        raise SheetNotFound(
            f"Sheet '{sheet}' not in snapshot. Available: {[s['name'] for s in snap['sheets']]}"
        )

    cells = target["cells"]
    if formulas_only:
        cells = [c for c in cells if c.get("formula")]
    return {
        "source": "stored snapshot (no re-parse)",
        "sheet": target["name"],
        "cell_count": len(target["cells"]),
        "returned": min(len(cells), limit),
        "total_matching": len(cells),
        "cells": cells[:limit],
        "data_validations": target["data_validations"],
        "text_boxes": target["text_box_notes"],
        "comments": target["comments"],
    }


# --- Layer 2: structure persistence + re-run + impact ----------------------

def _text(v) -> str | None:
    return None if v is None else str(v)


def persist_structure(version_id: str, result: StructureResult) -> dict:
    """Write the detected structure to the 5 Layer-2 tables.

    Metric rows get client-side UUIDs so parent_metric_row_id (self-FK) and
    template_fields.metric_row_id can be set without a second pass. Rows are
    inserted ordered by (sheet, row) so a parent always precedes its children.
    """
    sheet_id = sb.get_sheet_id_map(version_id)
    ordered = sorted(result.metric_rows, key=lambda r: (r.sheet_name, r.row))
    row_uuid: dict[tuple[str, int], str] = {
        (r.sheet_name, r.row): str(uuid.uuid4()) for r in ordered
    }

    metric_dicts = []
    for r in ordered:
        parent_id = (
            row_uuid.get((r.sheet_name, r.parent_row))
            if r.parent_row is not None
            else None
        )
        metric_dicts.append({
            "id": row_uuid[(r.sheet_name, r.row)],
            "template_version_id": version_id,
            "template_sheet_id": sheet_id.get(r.sheet_name),
            "sheet_name": r.sheet_name,
            "row": r.row,
            "label_text": r.label_text,
            "label_cell": r.label_cell,
            "label_col": r.label_col,
            "indent_level": r.indent_level,
            "parent_metric_row_id": parent_id,
            "data_cols": r.data_cols,
            "data_range": r.data_range,
            "is_formula": r.is_formula,
            "is_bold": r.is_bold,
            "is_strikethrough": r.is_strikethrough,
            "unit": r.unit,
            "number_format": r.number_format,
            "sample_value": _text(r.sample_value),
            "named_range": r.named_range,
        })
    sb.replace_rows("template_metric_rows", version_id, metric_dicts)

    period_dicts = [{
        "template_version_id": version_id,
        "template_sheet_id": sheet_id.get(p.sheet_name),
        "sheet_name": p.sheet_name,
        "col": p.col,
        "row": p.row,
        "label": p.label,
        "parsed_date": p.parsed_date,
        "period_type": p.period_type,
        "status": p.status.value,
    } for p in result.periods]
    sb.replace_rows("template_periods", version_id, period_dicts)

    field_dicts = [{
        "template_version_id": version_id,
        "metric_row_id": row_uuid.get((f.sheet_name, f.row)),
        "template_sheet_id": sheet_id.get(f.sheet_name),
        "sheet_name": f.sheet_name,
        "row": f.row,
        "label_text": f.label_text,
        "label_cell": f.label_cell,
        "input_columns": f.input_columns,
        "formula_columns": f.formula_columns,
        "current_period_col": f.current_period_col,
        "current_period_label": f.current_period_label,
        "is_unlocked": f.is_unlocked,
        "needs_collection": f.needs_collection,
        "has_historical_data": f.has_historical_data,
        "unit": f.unit,
        "number_format": f.number_format,
        "sample_value": _text(f.sample_value),
        "named_range": f.named_range,
        "indent_level": f.indent_level,
        "dependent_formulas": f.dependent_formulas,
        "downstream_cells": f.downstream_cells,
        "input_evidence": f.input_evidence,
    } for f in result.input_fields]
    sb.replace_rows("template_fields", version_id, field_dicts)

    region_dicts = [{
        "template_version_id": version_id,
        "template_sheet_id": sheet_id.get(r.sheet_name),
        "sheet_name": r.sheet_name,
        "cell_range": r.cell_range,
        "min_row": r.min_row, "min_col": r.min_col,
        "max_row": r.max_row, "max_col": r.max_col,
        "region_type": r.region_type,
        "cell_count": r.cell_count,
        "formula_count": r.formula_count,
        "input_count": r.input_count,
        "label_count": r.label_count,
    } for r in result.regions]
    sb.replace_rows("template_regions", version_id, region_dicts)

    signal_dicts = [{
        "template_version_id": version_id,
        "template_sheet_id": sheet_id.get(s.sheet_name),
        "sheet_name": s.sheet_name,
        "row": s.row,
        "text": s.text,
        "signal_type": s.signal_type,
    } for s in result.section_signals]
    sb.replace_rows("template_section_signals", version_id, signal_dicts)

    return {
        "metric_rows": len(metric_dicts),
        "periods": len(period_dicts),
        "input_fields": len(field_dicts),
        "regions": len(region_dicts),
        "section_signals": len(signal_dicts),
    }


def run_structure(template_id: str) -> dict:
    """Re-derive structure from the stored snapshot — no Aspose re-parse."""
    version_id, _, _ = sb.get_latest_file(template_id)
    blob = sb.download_snapshot(version_id)
    snap = json.loads(gzip.decompress(blob))
    parsed = reconstruct_workbook_from_snapshot(snap)
    counts = persist_structure(version_id, detect_structure(parsed))
    return {"template_version_id": version_id, "structure": counts}


def get_structure(template_id: str, sheet: str | None = None) -> dict:
    """Read the persisted Layer-2 structure (optionally one sheet)."""
    version_id, _, _ = sb.get_latest_file(template_id)
    client = sb.get_client()

    def q(table: str) -> list[dict]:
        b = client.table(table).select("*").eq("template_version_id", version_id)
        if sheet:
            b = b.eq("sheet_name", sheet)
        return b.execute().data or []

    return {
        "template_version_id": version_id,
        "metric_rows": q("template_metric_rows"),
        "fields": q("template_fields"),
        "periods": q("template_periods"),
        "section_signals": q("template_section_signals"),
    }


def _cell_rc(qualified: str) -> tuple[str, int, int] | None:
    """'Sheet!E12' -> ('Sheet', 12, 5)."""
    sheet, addr = _split_qualified(qualified)
    b = _range_bounds(addr)
    if not b:
        return None
    return sheet, b[0], b[1]


def build_dependents_index(snap: dict) -> dict[str, list[tuple[tuple[int, int, int, int], str]]]:
    """precedent-range sheet → [(bounds, target_formula_cell)] from a snapshot."""
    index: dict[str, list[tuple[tuple[int, int, int, int], str]]] = {}
    for sd in snap.get("sheets", []):
        for c in sd.get("cells", []):
            if not c.get("formula"):
                continue
            target = f"{sd['name']}!{c['address']}"
            for rng in c.get("precedents", []):
                rsheet, local = _split_qualified(rng)
                b = _range_bounds(local)
                if b:
                    index.setdefault(rsheet, []).append((b, target))
    return index


def trace_impact(
    index: dict[str, list[tuple[tuple[int, int, int, int], str]]],
    output_cells: set[str],
    labels: dict[tuple[str, int], str],
    start: str,
    depth: int = 3,
    max_total: int = 50,
) -> dict:
    """Pure forward BFS over the dependency index — no I/O. Returns the cells
    that recompute when ``start`` changes, flagging outputs + metric labels."""
    def dependents_of(qualified: str) -> list[str]:
        rc = _cell_rc(qualified)
        if not rc:
            return []
        _, row, col = rc
        return [
            target
            for (r1, c1, r2, c2), target in index.get(rc[0], [])
            if r1 <= row <= r2 and c1 <= col <= c2
        ]

    visited = {start}
    queue: list[tuple[str, int]] = [(start, 0)]
    affected: list[dict] = []
    truncated = False
    while queue:
        cur, d = queue.pop(0)
        if d >= depth:
            continue
        for dep in dependents_of(cur):
            if dep in visited:
                continue
            visited.add(dep)
            rc = _cell_rc(dep)
            affected.append({
                "cell": dep,
                "sheet": rc[0] if rc else None,
                "row": rc[1] if rc else None,
                "label": labels.get((rc[0], rc[1])) if rc else None,
                "is_output": dep in output_cells,
            })
            if len(affected) >= max_total:
                truncated = True
                break
            queue.append((dep, d + 1))
        if truncated:
            break

    return {
        "cell": start,
        "affected_count": len(affected),
        "truncated": truncated,
        "outputs_hit": [a for a in affected if a["is_output"]],
        "affected": affected,
    }


def impact(template_id: str, cell: str, depth: int = 3, max_total: int = 50) -> dict:
    """Deterministic 'change this cell → what's affected' over the dependency
    graph (from the snapshot). Maps each affected cell to its metric-row label."""
    version_id, _, _ = sb.get_latest_file(template_id)
    snap = json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    output_cells = set(snap.get("formula_graph", {}).get("output_cells", []))
    index = build_dependents_index(snap)

    labels: dict[tuple[str, int], str] = {}
    res = (
        sb.get_client()
        .table("template_metric_rows")
        .select("sheet_name, row, label_text")
        .eq("template_version_id", version_id)
        .execute()
    )
    for r in (res.data or []):
        labels[(r["sheet_name"], r["row"])] = r["label_text"]

    return trace_impact(index, output_cells, labels, cell, depth=depth, max_total=max_total)
