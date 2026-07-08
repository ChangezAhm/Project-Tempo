"""Offline tests for the review-items backend (no Supabase, no LLM)."""

from app.review.items import build_items_from_understanding, make_item
from app.review.verify import run_check


# --- make_item: content-addressed key ---------------------------------------

def test_make_item_key_stable():
    a = make_item(source="understanding", kind="judgment", question="Check X")
    b = make_item(source="understanding", kind="machine_checkable", question="Check X",
                  why="different metadata", affected={"sheets": ["S"]})
    assert a["item_key"] == b["item_key"]          # key ignores everything but source|question
    assert len(a["item_key"]) == 16

    c = make_item(source="understanding", kind="judgment", question="Check Y")
    d = make_item(source="triage", kind="judgment", question="Check X")
    assert c["item_key"] != a["item_key"]           # question changes the key
    assert d["item_key"] != a["item_key"]           # source changes the key


# --- build_items_from_understanding ------------------------------------------

def _workbook():
    return {
        "review_flags": ["EBITDA definition ambiguous on PortCo_Input"],
        "impact_chains": [
            {"name": "EBITDA → Valuation", "start": "Input!E12",
             "flows_to": ["Valuation", "Output!B3"],
             "significance": "drives the headline multiple",
             "graph_supported": False},
            {"name": "Supported chain", "start": "Input!A1",
             "flows_to": ["Calc"], "significance": "x", "graph_supported": True},
            {"name": "Unchecked chain", "start": "Input!A2",
             "flows_to": ["Calc"], "significance": "x", "graph_supported": None},
        ],
        "data_flow": [
            {"from_sheet": "Input", "to_sheet": "Valuation",
             "what": "Adjusted EBITDA", "graph_supported": False},
            {"from_sheet": "Input", "to_sheet": "Calc",
             "what": "raw figures", "graph_supported": True},
        ],
    }


def test_build_items_flags_become_judgment():
    items = build_items_from_understanding(_workbook())
    judgments = [i for i in items if i["kind"] == "judgment"]
    assert len(judgments) == 1
    j = judgments[0]
    assert j["question"] == "EBITDA definition ambiguous on PortCo_Input"
    assert j["source"] == "understanding"
    assert j["check_spec"] is None


def test_build_items_unsupported_chain_is_machine_checkable():
    items = build_items_from_understanding(_workbook())
    chains = [i for i in items
              if (i.get("check_spec") or {}).get("type") == "impact_chain"]
    assert len(chains) == 1                         # True/None chains produce nothing
    ch = chains[0]
    assert ch["kind"] == "machine_checkable"
    assert ch["check_spec"] == {"type": "impact_chain", "start": "Input!E12",
                                "flows_to": ["Valuation", "Output!B3"]}
    assert ch["why"] == "drives the headline multiple"
    assert ch["affected"] == {"sheets": ["Output", "Valuation"]}
    assert "EBITDA → Valuation" in ch["question"]
    assert "Input!E12" in ch["question"]


def test_build_items_unsupported_flow_is_machine_checkable():
    items = build_items_from_understanding(_workbook())
    flows = [i for i in items
             if (i.get("check_spec") or {}).get("type") == "sheet_flow"]
    assert len(flows) == 1
    f = flows[0]
    assert f["kind"] == "machine_checkable"
    assert f["check_spec"] == {"type": "sheet_flow",
                               "from_sheet": "Input", "to_sheet": "Valuation"}
    assert f["affected"] == {"sheets": ["Input", "Valuation"]}
    assert "Adjusted EBITDA" in f["question"]


def test_build_items_supported_claims_produce_nothing():
    items = build_items_from_understanding({
        "review_flags": [],
        "impact_chains": [{"name": "n", "start": "S!A1", "flows_to": ["T"],
                           "significance": "s", "graph_supported": True}],
        "data_flow": [{"from_sheet": "A", "to_sheet": "B", "what": "w",
                       "graph_supported": None}],
    })
    assert items == []


# --- run_check: sheet_flow ----------------------------------------------------

def _flow_snapshot():
    return {
        "sheets": [
            {"name": "In", "cells": [
                {"address": "A1", "formula": None, "precedents": []},
            ]},
            {"name": "Out", "cells": [
                {"address": "B2", "formula": "=In!A1", "precedents": ["In!A1"]},
                {"address": "B3", "formula": "=B2*2", "precedents": ["Out!B2"]},
            ]},
        ]
    }


def test_sheet_flow_verified():
    status, ev = run_check(
        {"type": "sheet_flow", "from_sheet": "In", "to_sheet": "Out"},
        _flow_snapshot(),
    )
    assert status == "verified"
    assert ev["edge_count"] == 1
    assert ev["sample_refs"] == ["Out!B2 <- In!A1"]


def test_sheet_flow_refuted():
    status, ev = run_check(
        {"type": "sheet_flow", "from_sheet": "Out", "to_sheet": "In"},
        _flow_snapshot(),
    )
    assert status == "refuted"
    assert ev == {"searched_edges": 1}              # only the one cross-sheet edge existed


# --- run_check: impact_chain ---------------------------------------------------
# Inp!A1 → Calc!B1 → Outp!C1 (precedents drive build_dependents_index, which
# only indexes cells that HAVE a formula).

def _chain_snapshot():
    return {
        "sheets": [
            {"name": "Inp", "cells": [
                {"address": "A1", "formula": None, "precedents": []},
            ]},
            {"name": "Calc", "cells": [
                {"address": "B1", "formula": "=Inp!A1", "precedents": ["Inp!A1"]},
            ]},
            {"name": "Outp", "cells": [
                {"address": "C1", "formula": "=Calc!B1", "precedents": ["Calc!B1"]},
            ]},
        ]
    }


def test_impact_chain_verified_sheet_and_cell_targets():
    status, ev = run_check(
        {"type": "impact_chain", "start": "Inp!A1", "flows_to": ["Outp", "Outp!C1"]},
        _chain_snapshot(),
    )
    assert status == "verified"
    assert ev["reached"]["Outp"] == ["Outp!C1"]
    assert ev["reached"]["Outp!C1"] == ["Outp!C1"]
    assert ev["not_reached"] == []
    assert ev["closure_size"] == 2                  # Calc!B1 and Outp!C1


def test_impact_chain_refuted():
    status, ev = run_check(
        {"type": "impact_chain", "start": "Inp!A1", "flows_to": ["Nowhere"]},
        _chain_snapshot(),
    )
    assert status == "refuted"
    assert ev["not_reached"] == ["Nowhere"]
    assert ev["reached"] == {}


def test_impact_chain_mixed_is_inconclusive():
    status, ev = run_check(
        {"type": "impact_chain", "start": "Inp!A1", "flows_to": ["Outp", "Nowhere"]},
        _chain_snapshot(),
    )
    assert status == "inconclusive"
    assert "Outp" in ev["reached"]
    assert ev["not_reached"] == ["Nowhere"]


def test_impact_chain_unparseable_start_is_inconclusive():
    status, ev = run_check(
        {"type": "impact_chain", "start": "the EBITDA input", "flows_to": ["Outp"]},
        _chain_snapshot(),
    )
    assert status == "inconclusive"
    assert ev == {"reason": "start is not a cell reference"}


def test_impact_chain_quoted_start_is_tolerated():
    status, _ = run_check(
        {"type": "impact_chain", "start": "'Inp'!A1", "flows_to": ["Outp"]},
        _chain_snapshot(),
    )
    assert status == "verified"


# --- run_check: unknown type ----------------------------------------------------

def test_unknown_check_type_is_inconclusive():
    status, ev = run_check({"type": "sniff_test"}, {"sheets": []})
    assert status == "inconclusive"
    assert ev == {"reason": "no verifier for this check type"}
