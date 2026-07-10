from datetime import date

from app.population.apply import apply_links
from app.population.run import _is_clearable_value, _pick_timeline, render_filled
from app.population.schema import CellLink


def _tiny_template(tmp_path):
    """A template copy with one stale LITERAL (B2), one connector-style FORMULA
    with a cached value (B3), and one label (A2) — the reset surface."""
    from aspose.cells import Workbook
    wb = Workbook()
    ws = wb.worksheets[0]
    ws.name = "S"
    ws.cells.get("A2").put_value("Revenue")
    ws.cells.get("B2").put_value(8.2)            # stale literal from a prior fill
    ws.cells.get("B3").formula = "=1000*2"       # connector-style formula cell
    wb.calculate_formula()
    p = tmp_path / "t.xlsx"
    wb.save(str(p))
    return p


def _clear_facts():
    return [{"sheet_name": "S", "cell": "B2"}, {"sheet_name": "S", "cell": "B3"}]


def _open(data: bytes, tmp_path):
    from aspose.cells import Workbook
    p = tmp_path / "out.xlsx"
    p.write_bytes(data)
    return Workbook(str(p)).worksheets[0]


def test_reset_values_clears_literals_only(tmp_path):
    data, stats, applied, skipped, _checks = render_filled(_tiny_template(tmp_path), [], _clear_facts())
    assert stats == {"cleared_values": 1, "cleared_formulas": 0}
    ws = _open(data, tmp_path)
    assert ws.cells.get("B2").value is None          # stale literal wiped
    assert ws.cells.get("B3").is_formula             # formula (cached stale) survives
    assert ws.cells.get("A2").value == "Revenue"     # labels never touched


def test_reset_full_clears_connector_formulas_too(tmp_path):
    data, stats, applied, skipped, _checks = render_filled(
        _tiny_template(tmp_path), [], _clear_facts(), reset="full")
    assert stats == {"cleared_values": 1, "cleared_formulas": 1}
    ws = _open(data, tmp_path)
    assert ws.cells.get("B2").value is None
    assert ws.cells.get("B3").value in (None, "")    # one file, one company
    assert ws.cells.get("A2").value == "Revenue"


def test_pick_timeline_prefers_monotonic_header_over_serial_data_row():
    # Row 8 is the real period header (Jan..Apr). Row 30 is a DATA row whose
    # values (in the 29k-60k range) parse as date serials AND it has more of
    # them — but they aren't monotonic by column, so it must not win.
    header = {3: date(2024, 1, 31), 4: date(2024, 2, 29), 5: date(2024, 3, 31)}
    data_row = {3: date(2010, 5, 1), 4: date(2031, 2, 3), 5: date(2001, 9, 9),
                6: date(2019, 1, 1), 7: date(2007, 6, 6)}
    assert _pick_timeline({8: header, 30: data_row}) == header


def test_pick_timeline_falls_back_to_densest_when_nothing_monotonic():
    a = {3: date(2024, 3, 1), 4: date(2024, 1, 1)}
    b = {3: date(2030, 1, 1), 4: date(2001, 1, 1), 5: date(2020, 1, 1)}
    assert _pick_timeline({1: a, 2: b}) == b


def _source():
    # A source workbook: row 5 = Revenue (C/D/E = three months, in thousands),
    # row 6 = Cost of Sales (positive), E6 empty.
    cells = [
        {"address": "B5", "value": "Revenue"},
        {"address": "C5", "value": 1000}, {"address": "D5", "value": 1100}, {"address": "E5", "value": 1200},
        {"address": "B6", "value": "Cost of sales"},
        {"address": "C6", "value": 400}, {"address": "D6", "value": 440},
    ]
    return {"sheets": [{"name": "MgmtAccts", "cells": cells}]}


def _facts():
    return [
        {"sheet_name": "PL", "cell": "AD20", "canonical_metric": "revenue", "metric_label": "Net Revenue", "period_index": 0, "scenario": "actual"},
        {"sheet_name": "PL", "cell": "AE20", "canonical_metric": "revenue", "metric_label": "Net Revenue", "period_index": 1, "scenario": "actual"},
        {"sheet_name": "PL", "cell": "AF20", "canonical_metric": "revenue", "metric_label": "Net Revenue", "period_index": 2, "scenario": "actual"},
        {"sheet_name": "PL", "cell": "AD21", "canonical_metric": "cost_of_sales", "metric_label": "Cost of Sales", "period_index": 0, "scenario": "actual"},
    ]


def _link(tc, sc, **kw):
    return CellLink(template_sheet="PL", template_cell=tc, source_sheet="MgmtAccts", source_cell=sc, **kw)


def test_apply_reads_values_deterministically_with_transform():
    links = [
        _link("AD20", "C5", unit_scale=0.001), _link("AE20", "D5", unit_scale=0.001),
        _link("AF20", "E5", unit_scale=0.001), _link("AD21", "C6", unit_scale=0.001),
    ]
    r = apply_links(_facts(), _source(), links, skipped=[])
    by_cell = {f.template_cell: f for f in r.filled}
    assert by_cell["AD20"].value == 1.0 and by_cell["AD20"].source_cell == "C5"
    assert by_cell["AE20"].value == 1.1 and by_cell["AF20"].value == 1.2
    assert by_cell["AD21"].value == 0.4 and by_cell["AD21"].source_cell == "C6"
    assert r.summary == {"facts": 4, "filled": 4, "unmatched": 0, "skipped": 0}


def test_sign_flip_and_sheet_prefix_and_range_are_normalised():
    # source cell cited as "MgmtAccts!C6:C6" with a sign flip (costs +ve in source).
    links = [_link("AD21", "MgmtAccts!C6:C6", unit_scale=0.001, sign_flip=True)]
    r = apply_links(_facts(), _source(), links, skipped=[])
    f = r.filled[0]
    assert f.template_cell == "AD21" and f.source_cell == "C6" and f.value == -0.4


def test_unmatched_empty_source_and_no_link():
    links = [_link("AD20", "C5", unit_scale=0.001), _link("AF20", "E6", unit_scale=0.001)]  # E6 empty
    r = apply_links(_facts(), _source(), links, skipped=[])
    reasons = {u["template_cell"]: u["reason"] for u in r.unmatched}
    assert reasons["AF20"] == "empty/absent source cell"
    assert reasons["AE20"] == "no source match"   # never linked
    assert reasons["AD21"] == "no source match"


def test_formula_or_error_source_value_is_not_written():
    src = {"sheets": [{"name": "MgmtAccts", "cells": [
        {"address": "C5", "value": "=TODECIMAL(IFBLANK(_BS!BC39,\"\"))"},   # a formula string
        {"address": "C6", "value": "#REF!"},                                # an error
    ]}]}
    facts = [
        {"sheet_name": "PL", "cell": "AD20", "canonical_metric": "revenue", "metric_label": "Revenue", "period_index": 0, "scenario": "actual"},
        {"sheet_name": "PL", "cell": "AD21", "canonical_metric": "cost_of_sales", "metric_label": "CoS", "period_index": 0, "scenario": "actual"},
    ]
    links = [_link("AD20", "C5"), _link("AD21", "C6")]
    r = apply_links(facts, src, links, skipped=[])
    assert r.filled == []   # neither formula text nor error is written
    reasons = {u["template_cell"]: u["reason"] for u in r.unmatched}
    assert "formula/error" in reasons["AD20"] and "formula/error" in reasons["AD21"]


def test_skipped_cells_are_not_unmatched():
    facts = _facts() + [
        {"sheet_name": "PL", "cell": "A1", "canonical_metric": None, "metric_label": "Budget", "period_index": 0, "scenario": "budget"},
    ]
    links = [_link("AD20", "C5", unit_scale=0.001)]
    skipped = [{"template_sheet": "PL", "template_cell": "A1", "reason": "column header, not an input"}]
    r = apply_links(facts, _source(), links, skipped=skipped)
    unmatched_cells = {u["template_cell"] for u in r.unmatched}
    assert "A1" not in unmatched_cells          # header was skipped, not unmatched
    assert r.summary["skipped"] == 1


def test_is_clearable_value():
    # stale numbers in input cells get wiped on refresh...
    assert _is_clearable_value(123, False) is True
    assert _is_clearable_value(1.5, False) is True
    assert _is_clearable_value(0, False) is True
    # ...but never formulas (computed/connector), text labels, blanks, or bools.
    assert _is_clearable_value(100, True) is False
    assert _is_clearable_value("Revenue", False) is False
    assert _is_clearable_value(None, False) is False
    assert _is_clearable_value(True, False) is False


def test_link_to_non_input_cell_is_not_written():
    # the model cites a template cell that isn't a known input — never write into it.
    links = [_link("AD20", "C5", unit_scale=0.001), _link("ZZ99", "D5")]
    r = apply_links(_facts(), _source(), links, skipped=[])
    assert {f.template_cell for f in r.filled} == {"AD20"}
    assert any(u.get("template_cell") == "ZZ99" and "not a known template input" in u["reason"]
               for u in r.unmatched)
