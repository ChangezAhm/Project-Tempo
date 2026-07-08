"""Routing: three-way pass (deep/light/skip), triage, and pass-scoped cache keys.

Offline — no API calls; _triage_ambiguous is exercised with a stubbed
guarded_stream. The load-bearing case is the balance rule: a tiny sheet with
input evidence must stay deep, no matter how few cells it has.
"""

import app.understanding.workbook as wb
from app.understanding.workbook import _sheet_cache_key, _triage_ambiguous, route_sheets


def _cell(addr, row, col, value, cell_type, formula=None, prec=None, style=None):
    return {"address": addr, "row": row, "col": col, "value": value, "formula": formula,
            "cell_type": cell_type, "precedents": prec or [], "style": style or {}}


def _text(addr, row, col, value):
    return _cell(addr, row, col, value, "string")


def _num(addr, row, col, value=1):
    return _cell(addr, row, col, value, "number")


def _snap(sheets, input_cells=(), named_ranges=()):
    return {
        "named_ranges": list(named_ranges),
        "formula_graph": {"input_cells": list(input_cells), "output_cells": []},
        "sheets": sheets,
    }


def _prose_sheet(name="Introduction", n=8):
    return {"name": name, "is_hidden": False,
            "cells": [_text(f"A{i}", i, 1, f"welcome text {i}") for i in range(1, n + 1)]}


def _routes(snap, **kw):
    return {r["sheet"]: r for r in route_sheets(snap, **kw)}


# --- the balance rule: never downgrade on size alone ------------------------

def test_tiny_input_rich_sheet_routes_deep():
    # 8 cells but 3 formula-graph inputs — could be the latest budget.
    snap = _snap(
        [{"name": "Budget", "is_hidden": False,
          "cells": [_num(f"A{i}", i, 1) for i in range(1, 9)]}],
        input_cells=["Budget!A1", "Budget!A2", "Budget!A3"],
    )
    r = _routes(snap)["Budget"]
    assert r["pass"] == "deep" and r["deep"] is True
    assert r["input_cells"] == 3


def test_tiny_numeric_sheet_without_signals_still_deep():
    # Bare numbers with no other signal can be hand-keyed inputs — not light.
    snap = _snap([{"name": "Assumptions", "is_hidden": False,
                   "cells": [_num(f"B{i}", i, 2) for i in range(1, 6)]}])
    assert _routes(snap)["Assumptions"]["pass"] == "deep"


def test_input_fill_style_counts_as_input_evidence():
    cells = [_cell("C3", 3, 3, 100, "number", style={"fill_color": "FFFFFF00"}),
             _text("A1", 1, 1, "Enter value:")]
    snap = _snap([{"name": "Entry", "is_hidden": False, "cells": cells}])
    r = _routes(snap)["Entry"]
    assert r["input_cells"] == 1
    assert r["pass"] == "deep"


# --- multi-signal inertness → light -----------------------------------------

def test_inert_prose_cover_routes_light():
    r = _routes(_snap([_prose_sheet()]))["Introduction"]
    assert r["pass"] == "light" and r["deep"] is False
    assert "inert" in r["reason"]
    assert r["triage"] is False


def test_validations_block_light():
    sheet = _prose_sheet("Feedback")
    sheet["data_validations"] = [{"sheet_name": "Feedback", "cell_range": "B2",
                                  "validation_type": "list"}]
    r = _routes(_snap([sheet]))["Feedback"]
    assert r["pass"] == "deep"


# --- skip rules unchanged ----------------------------------------------------

def test_hidden_empty_and_dump_still_skip():
    snap = _snap([
        {"name": "Hidden", "is_hidden": True, "cells": [_num("A1", 1, 1)]},
        {"name": "Empty", "is_hidden": False, "cells": []},
        {"name": "pbi_raw", "is_hidden": False,
         "cells": [_num(f"A{i}", i, 1) for i in range(1, 1001)]},
    ])
    by = _routes(snap)
    assert by["Hidden"]["pass"] == "skip" and by["Hidden"]["reason"] == "hidden"
    assert by["Empty"]["pass"] == "skip" and by["Empty"]["reason"] == "empty"
    assert by["pbi_raw"]["pass"] == "skip" and by["pbi_raw"]["reason"] == "data dump"
    assert all(by[n]["deep"] is False for n in ("Hidden", "Empty", "pbi_raw"))


# --- force_deep is always deep -----------------------------------------------

def test_force_deep_overrides_light():
    r = _routes(_snap([_prose_sheet()]), force_deep={"Introduction"})["Introduction"]
    assert r["pass"] == "deep" and r["deep"] is True
    assert r["reason"] == "forced by user"


# --- conflicted signals → triage marker, deep by default ----------------------

def _glossary_snap():
    # Glossary: read by a formula but computes nothing and takes no input —
    # signals conflict, so it must be flagged and stay deep until triage rules.
    return _snap([
        {"name": "Glossary", "is_hidden": False,
         "cells": [_text(f"A{i}", i, 1, f"term {i}: definition") for i in range(1, 9)]},
        {"name": "Calc", "is_hidden": False,
         "cells": [_cell("B1", 1, 2, "=Glossary!A1", "formula",
                         formula="=Glossary!A1", prec=["Glossary!A1"])]},
    ])


def test_conflicted_sheet_is_triage_flagged_and_deep():
    r = _routes(_glossary_snap())["Glossary"]
    assert r["triage"] is True
    assert r["pass"] == "deep" and r["deep"] is True


def test_triage_flips_to_light_on_clean_verdict(monkeypatch):
    snap = _glossary_snap()
    routes = route_sheets(snap)
    calls = []

    def fake(**kw):
        calls.append(kw)
        return None, '{"sheets": {"Glossary": "light"}}'

    monkeypatch.setattr(wb, "guarded_stream", fake)
    _triage_ambiguous(snap, routes)
    by = {r["sheet"]: r for r in routes}
    assert len(calls) == 1                      # ONE call for all ambiguous sheets
    assert calls[0]["model"] == wb.MODEL_MAP    # cheap tier
    assert by["Glossary"]["pass"] == "light" and by["Glossary"]["deep"] is False
    assert by["Glossary"]["reason"].startswith("triage:") and "light" in by["Glossary"]["reason"]
    assert by["Calc"]["pass"] == "deep"         # non-ambiguous routes untouched


def test_triage_garbage_reply_leaves_everything_deep(monkeypatch):
    snap = _glossary_snap()
    routes = route_sheets(snap)
    monkeypatch.setattr(wb, "guarded_stream", lambda **kw: (None, "sorry, not json"))
    _triage_ambiguous(snap, routes)
    assert {r["sheet"]: r for r in routes}["Glossary"]["pass"] == "deep"


def test_triage_call_failure_leaves_everything_deep(monkeypatch):
    snap = _glossary_snap()
    routes = route_sheets(snap)

    def boom(**kw):
        raise RuntimeError("api down")

    monkeypatch.setattr(wb, "guarded_stream", boom)
    _triage_ambiguous(snap, routes)
    assert {r["sheet"]: r for r in routes}["Glossary"]["pass"] == "deep"


def test_triage_noop_without_ambiguous_routes(monkeypatch):
    # No triage-flagged routes → no LLM call at all.
    def boom(**kw):
        raise AssertionError("must not be called")

    monkeypatch.setattr(wb, "guarded_stream", boom)
    snap = _snap([_prose_sheet()])
    routes = route_sheets(snap)
    _triage_ambiguous(snap, routes)


# --- cache keys are pass-scoped ------------------------------------------------

def test_cache_key_differs_between_pass_kinds():
    deep_key = _sheet_cache_key("v1", "Budget", "deep")
    light_key = _sheet_cache_key("v1", "Budget", "light")
    assert deep_key != light_key
    assert deep_key.endswith("-deep") and light_key.endswith("-light")
