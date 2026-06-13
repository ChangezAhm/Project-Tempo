"""Assemble inputs from the snapshot + workbook and run the per-sheet agent.

Loads the stored snapshot (grid + annotations + hints) and renders the sheet
image from the stored workbook, then calls understand_sheet. No re-parse via
Aspose except the image render.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
from pathlib import Path

from langsmith import traceable

from app import supabase_client as sb
from app.understanding.per_sheet import understand_sheet
from app.understanding.sheet_image import render_sheet_png

_CAP = 40  # cap list lengths fed to the prompt


def _annotations(sheet: dict) -> str:
    lines: list[str] = []
    for t in sheet.get("text_box_notes", [])[:_CAP]:
        near = ", ".join(t.get("nearby_labels", [])[:4])
        lines.append(
            f"- TextBox @{t.get('coverage_range') or t.get('anchor_cell')}: "
            f"\"{(t.get('text') or '').strip()[:300]}\""
            + (f"  (near: {near})" if near else "")
        )
    for v in sheet.get("data_validations", [])[:_CAP]:
        allowed = v.get("allowed_values") or []
        av = f" allowed={allowed[:12]}" if allowed else ""
        prompt = (v.get("prompt_message") or "").strip()
        pm = f" prompt=\"{prompt[:120]}\"" if prompt else ""
        lines.append(f"- Validation {v.get('cell_range')} type={v.get('validation_type')}{av}{pm}")
    for c in sheet.get("comments", [])[:_CAP]:
        lines.append(f"- Comment @{c.get('cell_address')}: \"{(c.get('text') or '').strip()[:200]}\"")
    return "\n".join(lines)


def _workbook_ctx(snap: dict) -> str:
    names = [s["name"] for s in snap.get("sheets", [])]
    nr = snap.get("named_ranges", [])
    nr_lines = [f"{n['name']} -> {', '.join(n.get('destinations', []))}" for n in nr[:60]]
    return (
        f"All sheets ({len(names)}): {names}\n"
        f"Named ranges ({len(nr)}): " + "; ".join(nr_lines) + "\n"
        "Reporting/as-of date: unknown (not yet extracted)"
    )


def _cross_sheet_counts(snap: dict, sheet_name: str) -> tuple[dict, dict]:
    """(reads_from, read_by): how this sheet's formulas reference other sheets,
    and how other sheets' formulas reference this one — from the dep graph."""
    reads_from: dict[str, int] = {}
    read_by: dict[str, int] = {}
    for s in snap.get("sheets", []):
        src = s["name"]
        for c in s.get("cells", []):
            for rng in c.get("precedents", []):
                i = rng.rfind("!")
                if i == -1:
                    continue
                ref = rng[:i].strip("'")
                if src == sheet_name and ref != sheet_name:
                    reads_from[ref] = reads_from.get(ref, 0) + 1
                elif ref == sheet_name and src != sheet_name:
                    read_by[src] = read_by.get(src, 0) + 1
    return reads_from, read_by


def _hints(snap: dict, sheet_name: str) -> str:
    g = snap.get("formula_graph", {})
    prefix = f"{sheet_name}!"
    inputs = sorted(a[len(prefix):] for a in g.get("input_cells", []) if a.startswith(prefix))
    nr_here = [
        n["name"]
        for n in snap.get("named_ranges", [])
        if any(prefix in d or f"'{sheet_name}'!" in d for d in n.get("destinations", []))
    ]
    sheet = next((s for s in snap["sheets"] if s["name"] == sheet_name), {})
    reads_from, read_by = _cross_sheet_counts(snap, sheet_name)
    top = lambda d: dict(sorted(d.items(), key=lambda kv: -kv[1])[:8])
    return (
        f"formula-graph input cells on this sheet ({len(inputs)}): "
        f"{inputs[:80]}{' …' if len(inputs) > 80 else ''}\n"
        f"named ranges on this sheet: {nr_here[:40]}\n"
        f"detected regions on this sheet: {len(sheet.get('regions', []))}\n"
        f"cross-sheet — this sheet READS FROM: {top(reads_from)}\n"
        f"cross-sheet — this sheet is READ BY: {top(read_by)}"
    )


@traceable(name="understand_template_sheet", run_type="chain")
def understand_template_sheet(template_id: str, sheet_name: str, *, max_tokens: int = 32000) -> dict:
    version_id, storage_path, filename = sb.get_latest_file(template_id)
    snap = json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    sheet = next((s for s in snap["sheets"] if s["name"] == sheet_name), None)
    if sheet is None:
        raise ValueError(f"Sheet '{sheet_name}' not in snapshot for template {template_id}")

    data = sb.download_workbook(storage_path)
    fd, name = tempfile.mkstemp(suffix=Path(filename).suffix or ".xlsx")
    os.close(fd)
    tmp = Path(name)
    try:
        tmp.write_bytes(data)
        image_png = render_sheet_png(tmp, sheet_name)
    finally:
        tmp.unlink(missing_ok=True)

    return understand_sheet(
        sheet,
        image_png,
        annotations=_annotations(sheet),
        workbook_ctx=_workbook_ctx(snap),
        hints=_hints(snap, sheet_name),
        max_tokens=max_tokens,
    )
