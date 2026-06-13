from app.pipeline import build_dependents_index, trace_impact


def test_trace_impact_chain():
    # A1 → B1 → C1 (B1=A1, C1=B1) on sheet S; C1 is the output.
    index = {
        "S": [
            ((1, 1, 1, 1), "S!B1"),  # B1 depends on A1
            ((1, 2, 1, 2), "S!C1"),  # C1 depends on B1
        ]
    }
    out = trace_impact(index, output_cells={"S!C1"}, labels={}, start="S!A1")
    cells = {a["cell"] for a in out["affected"]}
    assert cells == {"S!B1", "S!C1"}
    assert out["affected_count"] == 2
    assert [a["cell"] for a in out["outputs_hit"]] == ["S!C1"]


def test_trace_impact_depth_limit():
    index = {
        "S": [
            ((1, 1, 1, 1), "S!B1"),
            ((1, 2, 1, 2), "S!C1"),
            ((1, 3, 1, 3), "S!D1"),
        ]
    }
    out = trace_impact(index, output_cells=set(), labels={}, start="S!A1", depth=1)
    # depth=1 → only the direct dependent B1
    assert {a["cell"] for a in out["affected"]} == {"S!B1"}


def test_build_dependents_index_from_snapshot():
    snap = {
        "sheets": [
            {
                "name": "S",
                "cells": [
                    {"address": "A1", "row": 1, "col": 1, "formula": None},
                    {"address": "B1", "row": 1, "col": 2, "formula": "=A1", "precedents": ["S!A1"]},
                ],
            }
        ]
    }
    index = build_dependents_index(snap)
    assert index["S"] == [((1, 1, 1, 1), "S!B1")]


def test_label_annotation():
    index = {"S": [((1, 1, 1, 1), "S!B1")]}
    out = trace_impact(index, output_cells=set(), labels={("S", 1): "Revenue"}, start="S!A1")
    assert out["affected"][0]["label"] == "Revenue"
    assert out["affected"][0]["sheet"] == "S"
    assert out["affected"][0]["row"] == 1
