"""Tests for the range-based formula dependency graph (the precedent rewrite)."""

from app.raw_extraction.formula_mapper import (
    _range_bounds,
    _split_qualified,
    build_formula_graph_from_precedents,
)


def test_split_qualified():
    assert _split_qualified("Sheet1!B2") == ("Sheet1", "B2")
    assert _split_qualified("pbi_val!C2:C140") == ("pbi_val", "C2:C140")
    assert _split_qualified("B2") == ("", "B2")
    # splits on the LAST '!' so an odd sheet name with '!' keeps the address
    assert _split_qualified("Weird!Name!A1") == ("Weird!Name", "A1")


def test_range_bounds():
    assert _range_bounds("B2") == (2, 2, 2, 2)
    assert _range_bounds("B2:B742") == (2, 2, 742, 2)
    assert _range_bounds("A1:C3") == (1, 1, 3, 3)
    # normalises reversed ranges
    assert _range_bounds("C3:A1") == (1, 1, 3, 3)
    assert _range_bounds("not-a-range") is None


def test_single_range_expands_against_populated_cells():
    # B1 = SUM(A1:A3); only A1..A3 + B1 are populated.
    populated = {"S!A1", "S!A2", "S!A3", "S!B1"}
    graph = build_formula_graph_from_precedents(
        precedents_by_cell={"S!B1": ["S!A1:A3"]},
        formula_addrs={"S!B1"},
        populated_cells=populated,
        formula_strings={"S!B1": "=SUM(A1:A3)"},
    )
    assert graph.input_cells == {"S!A1", "S!A2", "S!A3"}
    assert graph.output_cells == {"S!B1"}
    # link kept at RANGE granularity (one link, not three)
    assert len(graph.links) == 1
    assert graph.links[0].source == "S!A1:A3"
    assert graph.links[0].target == "S!B1"


def test_whole_column_reference_does_not_explode():
    # A huge range (whole column, ~1M rows) must only yield the cells that
    # actually exist — proving the cap-free design can't blow up.
    populated = {"S!A1", "S!A5", "S!A1000", "S!Z9"}
    graph = build_formula_graph_from_precedents(
        precedents_by_cell={"S!Z9": ["S!A1:A1048576"]},
        formula_addrs={"S!Z9"},
        populated_cells=populated,
    )
    assert graph.input_cells == {"S!A1", "S!A5", "S!A1000"}
    assert graph.output_cells == {"S!Z9"}


def test_reference_to_empty_range_yields_nothing():
    graph = build_formula_graph_from_precedents(
        precedents_by_cell={"S!B1": ["S!X100:X200"]},
        formula_addrs={"S!B1"},
        populated_cells={"S!B1"},
    )
    assert graph.input_cells == set()
    assert graph.output_cells == {"S!B1"}


def test_cross_sheet_precedent():
    populated = {"Calc!B1", "Data!C2", "Data!C3"}
    graph = build_formula_graph_from_precedents(
        precedents_by_cell={"Calc!B1": ["Data!C2:C3"]},
        formula_addrs={"Calc!B1"},
        populated_cells=populated,
    )
    assert graph.input_cells == {"Data!C2", "Data!C3"}
