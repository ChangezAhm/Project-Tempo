"""Full raw-extraction snapshot (Option B storage model).

Serialises the entire in-memory ParsedWorkbook into one JSON document so the
complete extraction is captured durably (gzipped into Supabase Storage),
without bloating Postgres. Everything downstream — structure detection, the
LLM brief, the contract — reads from this snapshot instead of re-parsing.

The formula graph's expanded links are NOT stored (they're derivable from each
cell's `precedents`); only the input/output cell sets + a count are kept.
"""

from __future__ import annotations

from app.raw_extraction.workbook_parser import ParsedSheet, ParsedWorkbook

SNAPSHOT_SCHEMA_VERSION = 1


def _sheet_to_dict(s: ParsedSheet) -> dict:
    return {
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
        "narrow_columns": s.narrow_columns,
        "row_group_levels": {str(k): v for k, v in s.row_group_levels.items()},
        "cells": [c.model_dump(mode="json") for c in s.cells],
        "merged_ranges": [m.model_dump(mode="json") for m in s.merged_ranges],
        "regions": [r.model_dump(mode="json") for r in s.regions],
        "comments": [c.model_dump(mode="json") for c in s.comments],
        "data_validations": [v.model_dump(mode="json") for v in s.data_validations],
        "conditional_formats": [c.model_dump(mode="json") for c in s.conditional_formats],
        "text_box_notes": [t.model_dump(mode="json") for t in s.text_box_notes],
        "page_headers_footers": [p.model_dump(mode="json") for p in s.page_headers_footers],
        "chart_captions": [c.model_dump(mode="json") for c in s.chart_captions],
        "picture_notes": [p.model_dump(mode="json") for p in s.picture_notes],
        "hyperlinks": [h.model_dump(mode="json") for h in s.hyperlinks],
    }


def workbook_to_snapshot(parsed: ParsedWorkbook) -> dict:
    """The complete raw extraction as a JSON-serialisable dict."""
    g = parsed.formula_graph
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "metadata": parsed.metadata.model_dump(mode="json"),
        "named_ranges": [nr.model_dump(mode="json") for nr in parsed.named_ranges],
        "formula_graph": {
            "input_cells": sorted(g.input_cells),
            "output_cells": sorted(g.output_cells),
            "link_count": len(g.links),
        },
        "sheets": [_sheet_to_dict(s) for s in parsed.sheets],
    }
