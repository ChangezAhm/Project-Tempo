"""Verify-after-fill: check discovery, classification, and the recalculate-and-read
flow in render_filled (incl. the save-BEFORE-calc guarantee)."""

from app.population.run import render_filled
from app.population.template_checks import (
    checks_to_review_items,
    classify_check,
    collect_check_cells,
    summarize_checks,
)


# --- discovery ---------------------------------------------------------------
def test_discovery_check_named_sheet_collects_formula_cells_only():
    snap = {"sheets": [{"name": "Checks", "cells": [
        {"address": "B2", "row": 2, "col": 2, "value": '=IF(A1=A2,"OK","ERROR")',
         "formula": '=IF(A1=A2,"OK","ERROR")', "cached_value": "OK"},
        {"address": "B3", "row": 3, "col": 2, "value": "A literal note"},          # not a formula
        {"address": "B4", "row": 4, "col": 2, "value": "=A1-A2", "formula": "=A1-A2",
         "cached_value": 0.0},                                                      # numeric tie-out
    ]}]}
    checks = collect_check_cells(snap, None)
    by = {c["cell"]: c for c in checks}
    assert set(by) == {"B2", "B4"}
    assert by["B2"]["kind"] == "ok_error" and by["B2"]["origin"] == "check_sheet"
    assert by["B4"]["kind"] == "tie_out"


def test_discovery_reconciliation_section_and_formula_shape():
    snap = {"sheets": [{"name": "BS", "cells": [
        {"address": "C6", "row": 6, "col": 3, "value": "=B6-B7", "formula": "=B6-B7",
         "cached_value": 0.1},                                                      # inside recon section
        {"address": "C20", "row": 20, "col": 3, "value": "=SUM(B1:B9)", "formula": "=SUM(B1:B9)",
         "cached_value": 123.0},                                                    # plain sum outside
        {"address": "C21", "row": 21, "col": 3, "value": "=B1=B2", "formula": "=B1=B2",
         "cached_value": True},                                                     # boolean shape anywhere
        {"address": "C22", "row": 22, "col": 3, "value": "=CX_GET(1)", "formula": "=CX_GET(1)",
         "cached_value": "OK"},                                                     # connector: never a check
    ]}]}
    und = {"sheets": [{"sheet_name": "BS", "understanding": {"sections": [
        {"section_type": "reconciliation", "cell_range": "C5:C8"}]}}]}
    by = {c["cell"]: c for c in collect_check_cells(snap, und)}
    assert "C6" in by and by["C6"]["origin"] == "check_section" and by["C6"]["kind"] == "tie_out"
    assert "C21" in by and by["C21"]["kind"] == "boolean" and by["C21"]["origin"] == "formula_shape"
    assert "C20" not in by          # a plain numeric formula outside a check surface
    assert "C22" not in by          # connectors excluded


# --- classification ----------------------------------------------------------
def test_classify_check_matrix():
    assert classify_check(None, True, kind="boolean") == "pass"
    assert classify_check(None, False, kind="boolean") == "fail"
    assert classify_check(None, "OK", kind="ok_error") == "pass"
    assert classify_check(None, " pass ", kind="ok_error") == "pass"
    assert classify_check(None, "ERROR", kind="ok_error") == "fail"
    assert classify_check(None, "FAIL", kind="ok_error") == "fail"
    assert classify_check(None, "#NAME?", kind="ok_error") == "not_computable"
    assert classify_check(None, "#REF!", kind="boolean") == "not_computable"
    assert classify_check(None, 0.3, kind="tie_out") == "pass"
    assert classify_check(None, 12.0, kind="tie_out") == "fail"
    assert classify_check(None, "whatever", kind="ok_error") == "indeterminate"


def test_summary_and_review_items():
    results = [
        {"sheet": "Checks", "cell": "B2", "label": "Checks!B2", "kind": "ok_error",
         "before": "OK", "after": "ERROR", "status": "fail", "changed": True},
        {"sheet": "Checks", "cell": "B3", "label": "Checks!B3", "kind": "boolean",
         "before": True, "after": True, "status": "pass", "changed": False},
        {"sheet": "Checks", "cell": "B4", "label": "Checks!B4", "kind": "ok_error",
         "before": "OK", "after": "#NAME?", "status": "not_computable", "changed": True},
    ]
    s = summarize_checks(results)
    assert (s["evaluated"], s["passed"], s["failed"], s["not_computable"], s["changed"]) == (3, 1, 1, 1, 2)
    items = checks_to_review_items(results, "src.xlsx")
    assert len(items) == 1 and "Checks!B2" in items[0]["question"]
    # key is value-free: same cell fails with a different value -> same item_key
    results2 = [dict(results[0], after="MISMATCH")]
    assert checks_to_review_items(results2, "other.xlsx")[0]["item_key"] == items[0]["item_key"]


# --- the recalc flow in render_filled (Aspose integration) --------------------
def _check_template(tmp_path):
    from aspose.cells import Workbook
    wb = Workbook()
    ws = wb.worksheets[0]
    ws.name = "S"
    ws.cells.get("B2").put_value(0)                       # input cell (starts 0)
    ws.cells.get("C2").formula = '=IF(B2>0,"OK","ERROR")'  # template's own check
    wb.calculate_formula(True)                             # cache C2="ERROR" (pre-connector)
    ws.cells.get("D2").formula = "=CX_GET(1)"              # connector cell, cache left EMPTY
    p = tmp_path / "t.xlsx"
    wb.save(str(p))
    return p


class _FC:
    """Minimal FilledCell stand-in for render_filled."""
    def __init__(self, sheet, cell, value):
        self.template_sheet, self.template_cell, self.value = sheet, cell, value


def test_render_filled_recalculates_and_reads_checks(tmp_path):
    checks = [{"sheet": "S", "cell": "C2", "row": 2, "col": 3, "kind": "ok_error",
               "origin": "formula_shape", "label": "S!C2", "before": None}]
    data, stats, _a, _s, results, _l = render_filled(
        _check_template(tmp_path), [_FC("S", "B2", 5)], [], checks=checks)
    assert len(results) == 1
    r = results[0]
    assert r["status"] == "pass" and r["changed"] is True   # ERROR -> OK after the fill
    assert "calc_seconds" in stats
    # save-BEFORE-calc: the delivered file must contain no #NAME? residue
    from aspose.cells import Workbook
    p = tmp_path / "out.xlsx"
    p.write_bytes(data)
    ws = Workbook(str(p)).worksheets[0]
    sv = ws.cells.get("D2").value
    assert not (isinstance(sv, str) and sv.startswith("#"))


def test_render_filled_calc_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMPO_VERIFY_CALC", "0")
    checks = [{"sheet": "S", "cell": "C2", "row": 2, "col": 3, "kind": "ok_error",
               "origin": "formula_shape", "label": "S!C2", "before": None}]
    data, stats, _a, _s, results, _l = render_filled(
        _check_template(tmp_path), [_FC("S", "B2", 5)], [], checks=checks)
    assert results == [] and "calc_seconds" not in stats and data


def test_render_filled_calc_error_contained(tmp_path, monkeypatch):
    # Aspose native classes are immutable (can't patch calculate_formula), so the
    # containment is exercised via evaluate_checks inside the same try block.
    from app.population import template_checks as tc
    def boom(wb, checks):
        raise RuntimeError("calc exploded")
    monkeypatch.setattr(tc, "evaluate_checks", boom)
    checks = [{"sheet": "S", "cell": "C2", "row": 2, "col": 3, "kind": "ok_error",
               "origin": "formula_shape", "label": "S!C2", "before": None}]
    data, stats, _a, _s, results, _l = render_filled(
        _check_template(tmp_path), [_FC("S", "B2", 5)], [], checks=checks)
    assert data and results == [] and "calc exploded" in stats.get("calc_error", "")
