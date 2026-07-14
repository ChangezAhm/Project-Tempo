"""Construct the minimal understanding/structure/snapshot a case needs to run
through the REAL derive_data_model — so a golden case with known ground truth can
exercise the whole classification pipeline (input_fields → _emit → category)."""

from __future__ import annotations

from app.raw_extraction.column_utils import column_index


def cell(address, *, value=None, cached=None, formula=None, style=None):
    m = __import__("re").match(r"^([A-Z]+)(\d+)$", address.upper())
    col, row = column_index(m.group(1)), int(m.group(2))
    return {"address": address.upper(), "row": row, "col": col,
            "value": value, "cached_value": cached, "formula": formula,
            "style": style or {}}


def input_field(label, cells, needs_value=True):
    return {"label": label, "cells": list(cells), "needs_value": needs_value,
            "metric_row_label_cell": None, "notes": None, "confidence": 0.9, "evidence": []}


def build(sheet_name, cells, *, input_fields=None, role="input", input_surface=None,
          periods=None, metric_rows=None, sections=None):
    """Returns (understanding, structure, snapshot) for facts_for_inline."""
    used_r = max((c["row"] for c in cells), default=1)
    used_c = max((c["col"] for c in cells), default=1)
    snapshot = {"sheets": [{"name": sheet_name, "cells": cells,
                            "used_max_row": used_r, "used_max_col": used_c,
                            "is_protected": True, "data_validations": [],
                            "merged_ranges": [], "row_group_levels": {}}]}
    u = {"role": role, "label_columns": [1], "summary": "test sheet",
         "sections": sections or [], "metric_rows": metric_rows or [],
         "periods": periods or [], "input_fields": input_fields or [],
         "scenario_regions": [], "author_rules": [], "extensible_regions": []}
    understanding = {"available": True, "template_version_id": "v1",
                     "workbook": {"input_surface_sheets": input_surface or [sheet_name],
                                  "archetype": "test"},
                     "sheets": [{"sheet_name": sheet_name, "role": role, "understanding": u}]}
    structure = {"periods": [], "metric_rows": [], "fields": []}
    return understanding, structure, snapshot
