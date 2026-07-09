from datetime import datetime

from app.understanding.sheet_view import _body, _fmt, build_text_grid


def test_body_shows_result_and_formula():
    # formula date header -> BOTH its computed date (first) and the formula (braces):
    # the model needs values for dates/numbers AND formula text for logic
    # (sign conventions, cross-sheet flows).
    c = {"row": 4, "col": 3, "value": "=EOMONTH(AsOf,-1)",
         "formula": "=EOMONTH(AsOf,-1)", "cached_value": "2025-01-31T00:00:00"}
    assert _body(c) == "=2025-01-31 {=EOMONTH(AsOf,-1)}"
    assert _body({"formula": "=SUM(A1:A3)", "cached_value": 1234}) == "=1234 {=SUM(A1:A3)}"
    # no cached result -> the formula text alone (old behaviour preserved)
    assert _body({"value": "=X", "formula": "=X"}) == "=X"
    # a plain (non-formula) cell is unchanged
    assert _body({"value": "Revenue"}) == "Revenue"


def test_body_truncation_eats_formula_never_value():
    long_f = "=" + "+".join(f"A{i}" for i in range(1, 40))
    out = _body({"formula": long_f, "cached_value": 999})
    assert out.startswith("=999 {=A1+")      # value intact
    assert out.endswith("…}")                 # formula tail truncated inside braces


def test_fmt_trims_iso_datetime():
    assert _fmt("2025-01-31T00:00:00") == "2025-01-31"
    assert _fmt(datetime(2025, 3, 31)) == "2025-03-31"
    assert _fmt("Cost of Goods Sold") == "Cost of Goods Sold"


def test_grid_shows_date_header_value_and_formula():
    # end to end: a formula date header renders as its date AND keeps the formula
    cells = [_c("C4", 4, 3, "=EOMONTH(AsOf,-1)", "formula", formula="=EOMONTH(AsOf,-1)")]
    cells[0]["cached_value"] = "2025-01-31T00:00:00"
    g = build_text_grid(_sheet(cells))
    assert "C==2025-01-31 {=EOMONTH(AsOf,-1)}" in g


def _sheet(cells, **kw):
    base = {
        "name": "PL", "is_protected": True, "used_max_row": 50, "used_max_col": 8,
        "cells": cells, "merged_ranges": [], "data_validations": [],
    }
    base.update(kw)
    return base


def _c(addr, row, col, value, ctype="string", formula=None, **style):
    return {"address": addr, "row": row, "col": col, "value": value,
            "formula": formula, "cell_type": ctype, "style": style}


def test_prefix_and_suffix_markers():
    cells = [
        _c("A4", 4, 1, "Revenue", bold=True),
        _c("B5", 5, 2, "Product", indent_level=1),
        _c("E4", 4, 5, 100, "number", is_locked=False),
        _c("F4", 4, 6, "=E4*2", "formula", formula="=E4*2"),
    ]
    g = build_text_grid(_sheet(cells))
    assert "A=*Revenue" in g                  # bold prefix, unquoted
    assert "B=›1 Product" in g                 # indent prefix
    assert "E=100 [unlocked]" in g             # unlocked suffix on protected sheet
    assert "F==E4*2" in g                       # formula (double =)
    assert g.index("r4:") < g.index("r5:")


def test_merged_marker():
    cells = [_c("B3", 3, 2, "P&L", bold=True)]
    s = _sheet(cells, merged_ranges=[
        {"range": "B3:H3", "min_row": 3, "min_col": 2, "max_row": 3, "max_col": 8, "value": "P&L"}
    ])
    assert "B=*P&L [mrg:B3:H3]" in build_text_grid(s)


def test_empty_validation_cell_is_surfaced():
    # E6 has no value (not in cells) but a validation covers it → must appear.
    cells = [_c("B6", 6, 2, "Headcount")]
    s = _sheet(cells, data_validations=[
        {"sheet_name": "PL", "cell_range": "E6:E6", "validation_type": "decimal", "allowed_values": []}
    ])
    g = build_text_grid(s)
    assert "E=[in]" in g
    assert "empty validation cells" in g       # header notes the injection


def test_formula_truncation_marker():
    long_formula = "=" + "+".join(f"A{i}" for i in range(1, 40))  # > 60 chars
    g = build_text_grid(_sheet([_c("C1", 1, 3, long_formula, "formula", formula=long_formula)]))
    assert "…" in g


def test_row_group_marker():
    cells = [_c("B6", 6, 2, "Product revenue", indent_level=1)]
    g = build_text_grid(_sheet(cells, row_group_levels={"6": 1}))
    assert "r6[grp:1]:" in g


def test_empty_unlocked_cell_rendered_ungated():
    # Sheet NOT protected, but an empty cell the author unlocked must still show.
    cells = [_c("E6", 6, 5, None, "empty", is_locked=False)]
    g = build_text_grid(_sheet(cells, is_protected=False))
    assert "E=[unlocked]" in g


def test_windows_wide_columns():
    cells = [_c("A1", 1, 1, "x"), _c("CV1", 1, 100, "far")]
    g = build_text_grid(_sheet(cells, used_max_col=100), max_cols=80)
    assert "columns beyond" in g
    assert "far" not in g
