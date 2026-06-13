from app.understanding.per_sheet import to_strict_schema
from app.understanding.schema import DataFlowEdge, ImpactChain, WorkbookUnderstanding
from app.understanding.workbook import _cross_sheet_edges, route_sheets, verify


def _num(addr, row, col):
    return {"address": addr, "row": row, "col": col, "value": 1, "formula": None,
            "cell_type": "number", "precedents": [], "style": {}}


def _formula(addr, row, col, f, prec):
    return {"address": addr, "row": row, "col": col, "value": f, "formula": f,
            "cell_type": "formula", "precedents": prec, "style": {}}


def _snap():
    return {
        "named_ranges": [],
        "formula_graph": {"input_cells": [], "output_cells": []},
        "sheets": [
            {"name": "pbi_data", "is_hidden": False, "cells": [_num(f"A{i}", i, 1) for i in range(1, 1001)]},
            {"name": "Calc", "is_hidden": False, "cells": [_formula("B1", 1, 2, "=pbi_data!A1", ["pbi_data!A1"])]},
            {"name": "Input", "is_hidden": False, "cells": [_num("A1", 1, 1)]},
            {"name": "Hidden", "is_hidden": True, "cells": [_num("A1", 1, 1)]},
        ],
    }


def test_cross_sheet_edges():
    edges, read_by = _cross_sheet_edges(_snap())
    assert ("pbi_data", "Calc") in edges      # Calc reads from pbi_data → flow pbi_data→Calc
    assert read_by["pbi_data"] == 1


def test_route_skips_dumps_and_hidden():
    by = {r["sheet"]: r for r in route_sheets(_snap())}
    assert by["pbi_data"]["deep"] is False and by["pbi_data"]["reason"] == "data dump"
    assert by["Hidden"]["deep"] is False and by["Hidden"]["reason"] == "hidden"
    assert by["Calc"]["deep"] is True
    assert by["Input"]["deep"] is True


def _wb(flows, chains=None):
    return WorkbookUnderstanding(
        archetype="x", purpose="x", audience="x", summary="x",
        input_surface_sheets=["Input"], sheet_roles=[], data_flow=flows,
        metric_reconciliations=[], business_rules=[], impact_chains=chains or [], review_flags=[],
    )


def test_verify_marks_flows_against_graph():
    wb = _wb([
        DataFlowEdge(from_sheet="pbi_data", to_sheet="Calc", what="values", confidence=0.9),
        DataFlowEdge(from_sheet="Input", to_sheet="Calc", what="values", confidence=0.5),  # not in graph
    ])
    summary = verify(wb, _snap())
    by = {(e.from_sheet, e.to_sheet): e for e in wb.data_flow}
    assert by[("pbi_data", "Calc")].graph_supported is True
    assert by[("Input", "Calc")].graph_supported is False
    assert summary["data_flow"] == {"supported": 1, "total": 2}
    assert any("NOT supported" in f for f in wb.review_flags)


def test_verify_impact_chain_sheet_level_fallback():
    # Cell-level BFS from pbi_data!A500 reaches nothing (no cell references it), but
    # pbi_data->Calc IS a confirmed edge → supported via the sheet-level fallback.
    # Input!A1 has neither a cell path nor a sheet edge to Calc → unsupported.
    wb = _wb([], chains=[
        ImpactChain(name="fallback", start="pbi_data!A500", flows_to=["Calc"],
                    significance="x", confidence=0.8),
        ImpactChain(name="orphan", start="Input!A1", flows_to=["Calc"],
                    significance="x", confidence=0.8),
    ])
    summary = verify(wb, _snap())
    by = {c.name: c for c in wb.impact_chains}
    assert by["fallback"].graph_supported is True
    assert by["orphan"].graph_supported is False
    assert summary["impact_chains"] == {"supported": 1, "total": 2}
    assert any("Impact chains NOT confirmed" in f for f in wb.review_flags)


def test_workbook_schema_is_strict():
    s = to_strict_schema(WorkbookUnderstanding)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    assert s.get("$defs")
