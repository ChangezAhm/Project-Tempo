"""Offline proof of extensible-region detection: the JSON parse (fences
tolerated), the deterministic conversion (A1→col, header cells→value_cols with
dates, occupied-row rejection, capacity), the digest (row numbers + blank-row
runs + validations), and the per-sheet flow with a stubbed LLM including the
one corrective retry. No API calls, no Supabase.
"""

import json

import pytest

from app.authoring import regions as R
from app.authoring.regions import RegionOut, _cell_map, _convert, _digest, _parse, _rc


def _cell(addr, row, col, value=None, cached=None):
    return {"address": addr, "row": row, "col": col, "value": value, "cached_value": cached}


def _sheet():
    """A KPI block: period headers at row 10, two filled KPI rows, blank
    formatted add-slots at rows 31-38 (label col B, value cols E/F), and a
    total row at 39 whose SUM already spans the blanks."""
    cells = [
        _cell("B10", 10, 2, "KPIs"),
        _cell("E10", 10, 5, "2024-01-31"),
        _cell("F10", 10, 6, "2024-02-29"),
        _cell("B25", 25, 2, "Revenue"),
        _cell("E25", 25, 5, 100.0), _cell("F25", 25, 6, 110.0),
        _cell("B26", 26, 2, "EBITDA"),
        _cell("E26", 26, 5, 20.0), _cell("F26", 26, 6, 22.0),
        _cell("B39", 39, 2, "Total KPIs"),
        _cell("E39", 39, 5, "=SUM(E25:E38)", cached=120.0),
    ]
    for r in range(31, 39):   # blank-but-formatted add rows (kept by the parser
        cells += [_cell(f"B{r}", r, 2),   # only because they carry input styling)
                  _cell(f"E{r}", r, 5), _cell(f"F{r}", r, 6)]
    return {
        "name": "KPIs",
        "cells": cells,
        "data_validations": [{
            "cell_range": "B31:B38", "validation_type": "list",
            "allowed_values": ["Custom KPI"], "prompt_message": "one KPI per row",
        }],
    }


def _region(**over):
    base = dict(kind="kpi_list", label_col_cell="B31", row_start=31, row_end=38,
                total_row=39, value_header_cells=["E10", "F10"],
                rules="one KPI per row", confidence=0.9, evidence=["B25", "B39"])
    base.update(over)
    return RegionOut(**base)


# --- JSON parse ---------------------------------------------------------------

def test_parse_handles_fences_and_extra_keys():
    out = _parse('```json\n{"regions":[{"kind":"kpi_list","label_col_cell":"B31",'
                 '"row_start":31,"row_end":38,"total_row":39,'
                 '"value_header_cells":["E10"],"confidence":0.8,"surprise":true}],'
                 '"chatter":1}\n```')
    assert len(out.regions) == 1
    r = out.regions[0]
    assert r.label_col_cell == "B31" and r.row_start == 31 and r.total_row == 39


def test_parse_empty_regions_is_valid():
    assert _parse('{"regions":[]}').regions == []


def test_parse_raises_on_junk():
    with pytest.raises(ValueError):
        _parse("I could not find any regions, sorry.")


# --- A1 → (col, row) -----------------------------------------------------------

def test_rc_a1_to_col():
    assert _rc("B31") == (2, 31)
    assert _rc("AD20") == (30, 20)
    assert _rc("aa5") == (27, 5)
    assert _rc("") is None
    assert _rc("Sheet!B31") is None
    assert _rc("31B") is None


# --- Deterministic conversion ---------------------------------------------------

def test_convert_happy_path():
    row, reason = _convert(_region(), "KPIs", _cell_map(_sheet()))
    assert reason is None
    assert row["sheet_name"] == "KPIs" and row["kind"] == "kpi_list"
    assert row["label_col"] == 2                     # from B31
    assert row["row_start"] == 31 and row["row_end"] == 38
    assert row["total_row"] == 39
    # value columns resolved from the header cells' addresses + real cell dates
    assert row["value_cols"] == [{"col": 5, "parsed_date": "2024-01-31"},
                                 {"col": 6, "parsed_date": "2024-02-29"}]
    assert row["rules"] == "one KPI per row" and row["confidence"] == 0.9
    assert row["evidence"] == ["B25", "B39"]


def test_convert_value_header_without_date_keeps_col():
    # G10 doesn't exist in the snapshot → the column is kept, date null
    row, reason = _convert(_region(value_header_cells=["E10", "G10"]), "KPIs",
                           _cell_map(_sheet()))
    assert reason is None
    assert row["value_cols"] == [{"col": 5, "parsed_date": "2024-01-31"},
                                 {"col": 7, "parsed_date": None}]


def test_convert_rejects_occupied_rows():
    # rows 25/26 hold 'Revenue'/'EBITDA' labels — the model must not claim them
    row, reason = _convert(_region(label_col_cell="B25", row_start=25, row_end=38),
                           "KPIs", _cell_map(_sheet()))
    assert row is None and "occupied" in reason and "25" in reason


def test_convert_rejects_capacity_below_one():
    row, reason = _convert(_region(row_start=38, row_end=37), "KPIs", _cell_map(_sheet()))
    assert row is None and "capacity" in reason


def test_convert_rejects_total_row_inside_range():
    row, reason = _convert(_region(row_end=39), "KPIs", _cell_map(_sheet()))
    assert row is None and "total_row" in reason


def test_convert_rejects_bad_label_address():
    row, reason = _convert(_region(label_col_cell="nope"), "KPIs", _cell_map(_sheet()))
    assert row is None and "A1" in reason


def test_convert_normalises_unknown_kind_and_clamps_confidence():
    row, reason = _convert(_region(kind="Mystery Block", confidence=7.0), "KPIs",
                           _cell_map(_sheet()))
    assert reason is None
    assert row["kind"] == "other" and row["confidence"] == 1.0


# --- Digest ---------------------------------------------------------------------

def test_digest_exposes_row_numbers_labels_and_blank_runs():
    d = _digest(_sheet())
    assert "SHEET: KPIs" in d
    assert "B25='Revenue'" in d and "  25 |" in d      # row numbers + label addresses
    assert "B39='Total KPIs'" in d
    assert "rows 31-38" in d and "cols B,E,F" in d     # blank-but-formatted run
    assert "B31:B38" in d and "one KPI per row" in d   # data validation surfaced
    assert "E10=2024-01-31" in d                       # header band shows period cells


def test_digest_no_blank_or_validation_sections_when_absent():
    sheet = {"name": "Plain", "cells": [_cell("B25", 25, 2, "Revenue"),
                                        _cell("E25", 25, 5, 1.0)]}
    d = _digest(sheet)
    assert "BLANK-BUT-FORMATTED" not in d and "DATA VALIDATIONS" not in d


# --- Stubbed-LLM sheet flow -------------------------------------------------------

def _reply(payload: str):
    """A guarded_stream stub returning canned text (regions.py only uses [1])."""
    def fake(**kwargs):
        return None, payload
    return fake


def test_detect_sheet_regions_with_stubbed_llm(monkeypatch):
    good = {"kind": "kpi_list", "label_col_cell": "B31", "row_start": 31, "row_end": 38,
            "total_row": 39, "value_header_cells": ["E10", "F10"],
            "rules": "one KPI per row", "confidence": 0.9, "evidence": ["B39"]}
    bad = {**good, "label_col_cell": "B25", "row_start": 25}   # claims occupied rows
    monkeypatch.setattr(R, "guarded_stream", _reply(json.dumps({"regions": [good, bad]})))

    rows, skipped = R.detect_sheet_regions(_sheet())
    assert len(rows) == 1 and rows[0]["label_col"] == 2 and rows[0]["row_start"] == 31
    assert len(skipped) == 1 and "occupied" in skipped[0]


def test_detect_retries_once_on_unparseable_reply(monkeypatch):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return None, "sorry, no JSON here"
        return None, '{"regions":[]}'

    monkeypatch.setattr(R, "guarded_stream", fake)
    rows, skipped = R.detect_sheet_regions(_sheet())
    assert rows == [] and skipped == []
    assert len(calls) == 2
    # the retry carries the failed exchange as a conversation
    msgs = calls[1]["messages"]
    assert msgs[1]["role"] == "assistant" and "no JSON" in msgs[1]["content"]
    assert "ONLY the JSON object" in msgs[2]["content"]


def test_detect_raises_after_failed_retry(monkeypatch):
    monkeypatch.setattr(R, "guarded_stream", _reply("still not json"))
    with pytest.raises(ValueError):
        R.detect_sheet_regions(_sheet())
