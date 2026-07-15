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
    row, reasons = _convert(_region(), "KPIs", _cell_map(_sheet()))
    assert reasons == []
    assert row["sheet_name"] == "KPIs" and row["kind"] == "kpi_list"
    assert row["label_col"] == 2                     # from B31
    assert row["row_start"] == 31 and row["row_end"] == 38
    assert row["total_row"] == 39
    # value columns resolved from the header cells' addresses + real cell dates,
    # with header_label + left-to-right position (for positional matching)
    assert row["value_cols"] == [
        {"col": 5, "parsed_date": "2024-01-31", "header_label": "2024-01-31", "position": 0},
        {"col": 6, "parsed_date": "2024-02-29", "header_label": "2024-02-29", "position": 1}]
    # no explicit slots claimed → the whole range synthesizes blank slots
    assert [s["row"] for s in row["slots"]] == list(range(31, 39))
    assert all(s["mode"] == "blank" for s in row["slots"])
    assert row["rules"] == "one KPI per row" and row["confidence"] == 0.9
    assert row["evidence"] == ["B25", "B39"]


def test_convert_value_header_without_date_keeps_col():
    # G10 doesn't exist in the snapshot → the column is kept, date null
    row, reasons = _convert(_region(value_header_cells=["E10", "G10"]), "KPIs",
                            _cell_map(_sheet()))
    assert reasons == []
    assert row["value_cols"] == [
        {"col": 5, "parsed_date": "2024-01-31", "header_label": "2024-01-31", "position": 0},
        {"col": 7, "parsed_date": None, "header_label": None, "position": 1}]


def test_convert_drops_occupied_blank_claims_but_keeps_the_rest():
    # rows 25/26 hold 'Revenue'/'EBITDA' labels: those blank-claims DROP (with a
    # reason each); the genuinely free rows survive — offending rows, not regions.
    row, reasons = _convert(_region(label_col_cell="B25", row_start=25, row_end=38),
                            "KPIs", _cell_map(_sheet()))
    assert row is not None
    assert {s["row"] for s in row["slots"]} == set(range(27, 39))
    assert len(reasons) == 2 and all("occupied" in r for r in reasons)


def test_convert_placeholder_and_editable_acceptance_is_layered():
    from app.authoring.regions import SlotOut
    sheet = _sheet()
    sheet["cells"].append(_cell("B27", 27, 2, "Custom KPI 1"))      # placeholder text
    sheet["cells"].append(_cell("B28", 28, 2, "Net Revenue"))       # a REAL label
    cmap = _cell_map(sheet)
    r = _region(label_col_cell="B27", row_start=27, row_end=28, slots=[
        SlotOut(row=27, mode="placeholder"),
        SlotOut(row=28, mode="placeholder"),       # model claim, no corroboration
    ])
    row, reasons = _convert(r, "KPIs", cmap, signals={})
    slots = {s["row"]: s for s in row["slots"]}
    assert slots[27]["mode"] == "placeholder" and slots[27]["current_label"] == "Custom KPI 1"
    assert 28 not in slots and any("real" in x for x in reasons)     # real label protected
    # editable_label on a FIXED-statement kind needs a STRUCTURAL signal
    r2 = _region(kind="chart_of_accounts", label_col_cell="B28", row_start=28, row_end=28,
                 slots=[SlotOut(row=28, mode="editable_label")])
    row2, reasons2 = _convert(r2, "KPIs", cmap, signals={(28, 2): ["unlocked"]})
    assert row2 and row2["slots"][0]["mode"] == "editable_label"
    row3, reasons3 = _convert(r2, "KPIs", cmap, signals={})
    assert row3 is None and any("structural" in x for x in reasons3)
    # but a CONFIGURABLE metric list (kpi_list) accepts the label WITHOUT a signal —
    # the LLM's section-level judgment stands in for the per-cell structural signal
    r4 = _region(kind="kpi_list", label_col_cell="B28", row_start=28, row_end=28,
                 slots=[SlotOut(row=28, mode="editable_label")])
    row4, _ = _convert(r4, "KPIs", cmap, signals={})
    assert row4 and row4["slots"][0]["mode"] == "editable_label"
    assert row4["slots"][0]["current_label"] == "Net Revenue"


def test_convert_rejects_capacity_below_one():
    row, reasons = _convert(_region(row_start=38, row_end=37), "KPIs", _cell_map(_sheet()))
    assert row is None and "capacity" in reasons[0]


def test_convert_total_row_inside_range_drops_that_row_only():
    row, reasons = _convert(_region(row_end=39), "KPIs", _cell_map(_sheet()))
    assert row is not None
    assert 39 not in {s["row"] for s in row["slots"]}
    assert any("total row" in x for x in reasons)


def test_convert_rejects_bad_label_address():
    row, reasons = _convert(_region(label_col_cell="nope"), "KPIs", _cell_map(_sheet()))
    assert row is None and "A1" in reasons[0]


def test_convert_normalises_unknown_and_legacy_kinds_and_clamps_confidence():
    row, reasons = _convert(_region(kind="Mystery Block", confidence=7.0), "KPIs",
                            _cell_map(_sheet()))
    assert reasons == []
    assert row["kind"] == "other" and row["confidence"] == 1.0
    row2, _ = _convert(_region(kind="other_adjustments"), "KPIs", _cell_map(_sheet()))
    assert row2["kind"] == "adjustment_rows"        # legacy normalization


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


# --- Roll-up (spanning-subtotal) adjustment blocks --------------------------------

def _bridge_sheet():
    """An EBITDA bridge: five OCCUPIED adjustment labels (no dropdown, not blank)
    summed by 'Adjusted EBITDA' — the shape the LLM misses because the SUM range
    is invisible in text."""
    return {"name": "Monthly_Flash", "cells": [
        _cell("C10", 10, 3, "2024-01-31"), _cell("D10", 10, 4, "2024-02-29"),
        _cell("B24", 24, 2, "EBITDA BRIDGE"),
        _cell("B25", 25, 2, "Reported EBITDA"),
        _cell("C25", 25, 3, "=C22", cached=0.0),
        _cell("B26", 26, 2, "Restructuring"),
        _cell("B27", 27, 2, "Transaction & Deal Costs"),
        _cell("B28", 28, 2, "Share-based Compensation"),
        _cell("B29", 29, 2, "Management Fees"),
        _cell("B30", 30, 2, "Other One-off Items"),
        _cell("B31", 31, 2, "Adjusted EBITDA"),
        _cell("C31", 31, 3, "=C25+SUM(C26:C30)", cached=0.0),
        _cell("D31", 31, 4, "=D25+SUM(D26:D30)", cached=0.0),
    ]}


def _pnl_sheet():
    """A FIXED statement subtotal (Total Revenue = SUM of two revenue lines) —
    a roll-up, but NOT an adjustment list; the safety net must leave it alone."""
    return {"name": "P&L", "cells": [
        _cell("B14", 14, 2, "Product Revenue"), _cell("C14", 14, 3, 100.0),
        _cell("B15", 15, 2, "Services Revenue"), _cell("C15", 15, 3, 50.0),
        _cell("B16", 16, 2, "Total Revenue"), _cell("C16", 16, 3, "=SUM(C14:C15)", cached=150.0),
    ]}


def test_subtotal_blocks_detects_labelled_rollup_and_ignores_blank_runs():
    from app.authoring.regions import _subtotal_blocks
    blocks = _subtotal_blocks(_bridge_sheet())
    assert len(blocks) == 1
    b = blocks[0]
    assert (b["lo"], b["hi"], b["subtotal_row"], b["label_col"]) == (26, 30, 31, 2)
    assert b["value_cols"] == [3, 4]                       # both C and D summed
    assert b["subtotal_label"] == "Adjusted EBITDA"
    assert b["labelled_rows"] == [26, 27, 28, 29, 30]
    # the KPI sheet's blank add-slot run (rows 31-38, only 2 labelled of 14) is NOT
    # a labelled list -> not a roll-up block
    assert _subtotal_blocks(_sheet()) == []


def test_is_adjustment_block_discriminates_bridge_from_fixed_subtotal():
    from app.authoring.regions import _is_adjustment_block, _subtotal_blocks
    bridge = _subtotal_blocks(_bridge_sheet())[0]
    assert _is_adjustment_block(bridge, _bridge_sheet()) is True
    pnl = _subtotal_blocks(_pnl_sheet())[0]
    assert _is_adjustment_block(pnl, _pnl_sheet()) is False


def test_label_signals_and_digest_expose_the_rollup():
    from app.authoring.regions import _digest, _label_signals
    sig = _label_signals(_bridge_sheet())
    assert "summed_member" in sig[(26, 2)] and "summed_member" in sig[(30, 2)]
    d = _digest(_bridge_sheet())
    assert "ROLL-UP sums rows 26-30" in d                  # subtotal row annotated
    assert "B26='Restructuring' [summed_member]" in d      # member rows annotated


def test_safety_net_regions_adjustment_block_the_llm_missed(monkeypatch):
    # the LLM returns NOTHING; the deterministic net still yields the bridge region
    monkeypatch.setattr(R, "guarded_stream", _reply(json.dumps({"regions": []})))
    rows, skipped = R.detect_sheet_regions(_bridge_sheet())
    assert len(rows) == 1
    reg = rows[0]
    assert reg["kind"] == "adjustment_rows"
    assert reg["detection_source"] == "deterministic_subtotal"
    assert reg["total_row"] == 31 and reg["row_start"] == 26 and reg["row_end"] == 30
    assert {s["row"] for s in reg["slots"]} == {26, 27, 28, 29, 30}
    assert all(s["mode"] == "editable_label" for s in reg["slots"])
    assert [vc["col"] for vc in reg["value_cols"]] == [3, 4]      # headers C10/D10 resolved


def test_safety_net_leaves_fixed_statement_subtotals_alone(monkeypatch):
    monkeypatch.setattr(R, "guarded_stream", _reply(json.dumps({"regions": []})))
    rows, skipped = R.detect_sheet_regions(_pnl_sheet())
    assert rows == []                                    # Total Revenue is not an adjustment list


def test_safety_net_defers_to_llm_when_block_already_covered(monkeypatch):
    # LLM already claims the bridge as a kpi_list -> the net must NOT double-add
    claim = {"kind": "kpi_list", "label_col_cell": "B26", "row_start": 26, "row_end": 30,
             "total_row": 31, "value_header_cells": ["C10", "D10"],
             "slots": [{"row": r, "mode": "editable_label"} for r in range(26, 31)],
             "confidence": 0.9, "evidence": ["B31"]}
    monkeypatch.setattr(R, "guarded_stream", _reply(json.dumps({"regions": [claim]})))
    rows, skipped = R.detect_sheet_regions(_bridge_sheet())
    assert len(rows) == 1 and rows[0]["kind"] == "kpi_list"   # one region, the LLM's


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
    # a claim ENTIRELY over occupied real labels dies (every slot drops)
    bad = {**good, "label_col_cell": "B25", "row_start": 25, "row_end": 26, "total_row": None}
    monkeypatch.setattr(R, "guarded_stream", _reply(json.dumps({"regions": [good, bad]})))

    rows, skipped = R.detect_sheet_regions(_sheet())
    assert len(rows) == 1 and rows[0]["label_col"] == 2 and rows[0]["row_start"] == 31
    assert skipped and all("occupied" in s for s in skipped)


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


def test_digest_exposes_unstored_blank_row_gaps():
    # A KPI block whose free slots have NO stored cells (no styling) is invisible
    # to the blank-formatted section — the used-range gap section must show it.
    sheet = {"name": "KPI", "used_max_row": 40, "cells": [
        {"address": "B25", "row": 25, "col": 2, "value": "Custom KPIs"},
        {"address": "B40", "row": 40, "col": 2, "value": "Total"},
    ]}
    from app.authoring.regions import _digest
    d = _digest(sheet)
    assert "UNSTORED BLANK ROWS" in d
    assert "rows 26-39" in d


def test_sheet_understanding_accepts_payload_without_regions():
    # Old per-sheet cache entries (pre-regions schema) must still validate —
    # the field defaults to [] so a cached payload without it loads cleanly.
    from app.understanding.schema import SheetUnderstanding
    payload = {"sheet_name": "S", "role": "input", "label_columns": [2],
               "summary": "x", "sections": [], "metric_rows": [], "periods": [],
               "input_fields": [], "author_rules": []}
    u = SheetUnderstanding.model_validate(payload)
    assert u.extensible_regions == []


def test_understanding_claim_converts_through_verifier():
    # The onboarding path routes ExtensibleRegionClaim through the SAME
    # deterministic verifier as the standalone detector.
    from app.authoring.regions import RegionOut, _cell_map, _convert
    from app.understanding.schema import ExtensibleRegionClaim
    sheet = {"name": "KPI", "cells": [
        {"address": "E10", "row": 10, "col": 5, "value": "2026-01-31"},
        {"address": "B30", "row": 30, "col": 2, "value": "Add KPIs below:"},
    ]}
    claim = ExtensibleRegionClaim(kind="kpi_list", label_col_cell="B31",
                                  row_start=31, row_end=34, total_row=35,
                                  value_header_cells=["E10"], rules="one per row",
                                  confidence=0.9, evidence=["B30"])
    row, reasons = _convert(RegionOut(**claim.model_dump()), "KPI", _cell_map(sheet))
    assert reasons == [] and row["label_col"] == 2 and row["row_start"] == 31
    assert row["value_cols"] == [{"col": 5, "parsed_date": "2026-01-31",
                                  "header_label": "2026-01-31", "position": 0}]
    assert [s["mode"] for s in row["slots"]] == ["blank"] * 4
