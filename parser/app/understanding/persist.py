"""Phase 4 — persist the Layer-3 understanding and serve it to the UI.

Runs the workbook understanding, renders a focused snippet image of each
input sheet's input block, distils a ranked list of "critical input areas"
(with the business-logic interpretation that makes population mapping
reliable), and writes it all to Supabase. `get_understanding` reads it back
with time-limited signed image URLs for the browser.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from app import supabase_client as sb
from app.raw_extraction.column_utils import column_index, column_letter
from app.understanding.run import _annotations  # noqa: F401 (kept for parity / future use)
from app.understanding.sheet_image import render_sheet_png
from app.understanding.workbook import understand_workbook

logger = logging.getLogger(__name__)

_A1 = re.compile(r"^([A-Z]+)(\d+)$")
_MODEL = "claude-opus-4-8"


def _bounding_range(addresses: list[str], *, pad_up=2, pad_down=1, pad_left=1, pad_right=1) -> str | None:
    """Tightest A1 range covering all given cells/ranges, padded slightly so the
    snippet shows surrounding labels/headers. Returns None if nothing parses."""
    rows: list[int] = []
    cols: list[int] = []
    for addr in addresses:
        for part in str(addr).split(":"):
            m = _A1.match(part.strip().upper())
            if m:
                cols.append(column_index(m.group(1)))
                rows.append(int(m.group(2)))
    if not rows:
        return None
    r1, r2 = max(1, min(rows) - pad_up), max(rows) + pad_down
    c1, c2 = max(1, min(cols) - pad_left), max(cols) + pad_right
    return f"{column_letter(c1)}{r1}:{column_letter(c2)}{r2}"


def _snippet_range(u) -> str | None:
    addrs: list[str] = []
    for f in u.input_fields:
        addrs.extend(f.cells)
        if f.metric_row_label_cell:
            addrs.append(f.metric_row_label_cell)
    return _bounding_range(addrs)


def _critical_inputs(understandings, input_surface: set[str], snippet_paths: dict[str, str]) -> list[dict]:
    """Distil + rank the input fields into critical-input rows. Rows with a
    captured business-logic definition rank highest, then those still needing a
    value, then those on the designated input surface."""
    rows: list[dict] = []
    for u in understandings:
        by_label_cell = {m.label_cell: m for m in u.metric_rows}
        for f in u.input_fields:
            mr = by_label_cell.get(f.metric_row_label_cell) if f.metric_row_label_cell else None
            rows.append({
                "sheet_name": u.sheet_name,
                "label": f.label,
                "cells": list(f.cells),
                "definition": getattr(mr, "definition", None) if mr else None,
                "qualification_criteria": getattr(mr, "qualification_criteria", None) if mr else None,
                "expected_source": getattr(mr, "expected_source", None) if mr else None,
                "interpretation_source": (mr.interpretation_source.value if mr and mr.interpretation_source else None),
                "unit": getattr(mr, "unit", None) if mr else None,
                "needs_value": bool(f.needs_value),
                "snippet_path": snippet_paths.get(u.sheet_name),
                "_sort": (
                    0 if (mr and mr.definition) else 1,
                    0 if f.needs_value else 1,
                    0 if u.sheet_name in input_surface else 1,
                    -float(f.confidence or 0),
                ),
            })
    rows.sort(key=lambda r: r.pop("_sort"))
    for i, r in enumerate(rows):
        r["rank"] = i
    return rows


def understand_and_persist(template_id: str, *, max_sheets: int = 16) -> dict:
    version_id, storage_path, filename = sb.get_latest_file(template_id)
    job_id = sb.create_job(version_id, job_type="understand")
    try:
        out = understand_workbook(template_id, max_sheets=max_sheets)
        wb = out["workbook"]
        understandings = out["sheet_understandings"]
        input_surface = set(wb.input_surface_sheets or [])

        # Render a focused snippet of each analysed sheet's input block.
        data = sb.download_workbook(storage_path)
        fd, name = tempfile.mkstemp(suffix=Path(filename).suffix or ".xlsx")
        os.close(fd)
        tmp = Path(name)
        snippet_paths: dict[str, str] = {}
        try:
            tmp.write_bytes(data)
            for u in understandings:
                try:
                    png = render_sheet_png(tmp, u.sheet_name, cell_range=_snippet_range(u))
                    snippet_paths[u.sheet_name] = sb.upload_snippet(version_id, u.sheet_name, png)
                except Exception as e:  # noqa: BLE001 — a bad render shouldn't fail the run
                    logger.warning("snippet render failed for %s: %s", u.sheet_name, e)
        finally:
            tmp.unlink(missing_ok=True)

        # Persist workbook understanding, per-sheet understanding, critical inputs.
        sb.upsert_understanding(version_id, {
            "archetype": wb.archetype,
            "purpose": wb.purpose,
            "audience": wb.audience,
            "summary": wb.summary,
            "input_surface_sheets": wb.input_surface_sheets,
            "review_flags": wb.review_flags,
            "understanding": wb.model_dump(mode="json"),
            "verify": out["verify"],
            "usage": out["usage"],
            "model": _MODEL,
        })
        sb.replace_rows("template_sheet_understanding", version_id, [
            {
                "template_version_id": version_id,
                "sheet_name": u.sheet_name,
                "role": u.role.value,
                "summary": u.summary,
                "understanding": u.model_dump(mode="json"),
                "snippet_path": snippet_paths.get(u.sheet_name),
            }
            for u in understandings
        ])
        crit = _critical_inputs(understandings, input_surface, snippet_paths)
        sb.replace_rows("template_critical_inputs", version_id,
                        [{**c, "template_version_id": version_id} for c in crit])

        summary = {
            "template_version_id": version_id,
            "deep_sheets": out["deep_sheets"],
            "sheet_count": len(understandings),
            "failed_sheets": out.get("failed_sheets", []),
            "critical_input_count": len(crit),
            "verify": out["verify"],
            "usage": out["usage"],
        }
        sb.complete_job(job_id, summary)
        return summary
    except Exception as e:
        sb.fail_job(job_id, str(e))
        raise


def get_understanding(template_id: str) -> dict:
    version_id, _, _ = sb.get_latest_file(template_id)
    client = sb.get_client()

    wb = (
        client.table("template_understanding").select("*")
        .eq("template_version_id", version_id).limit(1).execute().data
    )
    if not wb:
        return {"template_version_id": version_id, "available": False}

    sheets = (
        client.table("template_sheet_understanding").select("*")
        .eq("template_version_id", version_id).execute().data or []
    )
    crit = (
        client.table("template_critical_inputs").select("*")
        .eq("template_version_id", version_id).order("rank").execute().data or []
    )
    for row in (*sheets, *crit):
        row["snippet_url"] = sb.signed_snippet_url(row.get("snippet_path"))

    return {
        "template_version_id": version_id,
        "available": True,
        "workbook": wb[0],
        "sheets": sheets,
        "critical_inputs": crit,
    }
