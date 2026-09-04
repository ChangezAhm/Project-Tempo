"""Traceable-deliverable tests: sheet-name planning, formula construction that
mirrors apply_links' arithmetic, and the Aspose end-to-end (import source
sheets, write formulas, per-cell verify, mismatch fallback)."""

from app.population.apply import apply_links
from app.population.linked import (
    cited_sheets, formula_for, plan_source_sheet_names,
)
from app.population.run import render_filled
from app.population.schema import CellLink


# --- pure: sheet-name planning ----------------------------------------------

def test_plan_names_prefix_and_collision_with_template():
    # a source sheet named like a template sheet still gets its own import
    m = plan_source_sheet_names(["PL", "BS"], existing=["PL", "BS", "Checks"])
    assert m == {"PL": "Source - PL", "BS": "Source - BS"}


def test_plan_names_illegal_chars_truncation_and_dedupe():
    long = "Management Accounts / FY24 [final]*"
    m = plan_source_sheet_names([long, "Management Accounts  FY24 fin"], existing=[])
    n1, n2 = m[long], m["Management Accounts  FY24 fin"]
    assert all(len(n) <= 31 for n in (n1, n2))
    assert not set("[]:*?/\\") & set(n1 + n2)
    assert n1 != n2                      # truncation collision resolved
    assert n2.endswith("(2)")


def test_plan_names_case_insensitive_against_existing():
    m = plan_source_sheet_names(["Data"], existing=["SOURCE - DATA"])
    assert m["Data"] != "Source - Data"
    assert m["Data"].endswith("(2)")


# --- pure: formula construction ---------------------------------------------

def _resolver(**names):
    table = {k.lower(): v for k, v in names.items()}
    return lambda n: table.get((n or "").strip().lower())


def _lk(**kw):
    base = dict(template_sheet="PL", template_cell="D9",
                source_sheet="Mgmt", source_cell="C5")
    base.update(kw)
    return CellLink(**base)


def test_formula_single_ref_scale_and_sign():
    r = _resolver(mgmt="Source - Mgmt")
    assert formula_for(_lk(), r) == "='Source - Mgmt'!C5"
    assert formula_for(_lk(unit_scale=0.001), r) == "='Source - Mgmt'!C5*0.001"
    assert (formula_for(_lk(unit_scale=0.001, sign_flip=True), r)
            == "=-('Source - Mgmt'!C5*0.001)")


def test_formula_aggregation_sum_and_avg():
    r = _resolver(mgmt="Source - Mgmt", other="Source - Other")
    lk = _lk(agg_source_cells=["D5", "Other!E9"])
    assert (formula_for(lk, r)
            == "=SUM('Source - Mgmt'!C5,'Source - Mgmt'!D5,'Source - Other'!E9)")
    lk2 = _lk(agg_source_cells=["D5"], agg_op="avg", unit_scale=100.0)
    assert (formula_for(lk2, r)
            == "=AVERAGE('Source - Mgmt'!C5,'Source - Mgmt'!D5)*100")


def test_formula_normalises_ranges_prefixes_and_quotes():
    # source_cell cited as a Sheet!range; sheet name containing an apostrophe
    r = _resolver(**{"o'brien": "Source - O'Brien"})
    lk = _lk(source_sheet="O'Brien", source_cell="O'Brien!C6:C6")
    assert formula_for(lk, r) == "='Source - O''Brien'!C6"


def test_formula_text_is_bare_reference():
    r = _resolver(mgmt="Source - Mgmt")
    lk = _lk(unit_scale=0.001, sign_flip=True)   # transforms ignored for text
    assert formula_for(lk, r, is_text=True) == "='Source - Mgmt'!C5"


def test_formula_unresolvable_sheet_returns_none():
    r = _resolver(mgmt="Source - Mgmt")
    assert formula_for(_lk(agg_source_cells=["Ghost!A1"]), r) is None
    assert formula_for(_lk(source_sheet="Ghost"), r) is None


def test_cited_sheets_dedupes_case_insensitively():
    lk = _lk(agg_source_cells=["D5", "MGMT!E5", "Other!A1"])
    assert cited_sheets(lk) == ["Mgmt", "Other"]


# --- Aspose end-to-end -------------------------------------------------------

def _template(tmp_path):
    from aspose.cells import Workbook
    wb = Workbook()
    ws = wb.worksheets[0]
    ws.name = "PL"
    ws.cells.get("A20").put_value("Net Revenue")
    ws.cells.get("A21").put_value("Cost of Sales")
    p = tmp_path / "template.xlsx"
    wb.save(str(p))
    return p


def _source_snapshot():
    cells = [
        {"address": "B5", "value": "Revenue", "row": 5, "col": 2},
        {"address": "C5", "value": 1000, "row": 5, "col": 3,
         "style": {"number_format": "#,##0"}},
        {"address": "D5", "value": 1100, "row": 5, "col": 4},
        {"address": "C6", "value": 400, "row": 6, "col": 3},
    ]
    return {"sheets": [{"name": "PL", "cells": cells}]}   # same name as template's PL


def _facts():
    return [
        {"sheet_name": "PL", "cell": "D20", "canonical_metric": "revenue",
         "metric_label": "Net Revenue", "period_index": 0, "scenario": "actual"},
        {"sheet_name": "PL", "cell": "D21", "canonical_metric": "cost_of_sales",
         "metric_label": "Cost of Sales", "period_index": 0, "scenario": "actual"},
    ]


def _links():
    return [
        CellLink(template_sheet="PL", template_cell="D20", source_sheet="PL",
                 source_cell="C5", agg_source_cells=["D5"], unit_scale=0.001),
        CellLink(template_sheet="PL", template_cell="D21", source_sheet="PL",
                 source_cell="C6", unit_scale=0.001, sign_flip=True),
    ]


def _open(data: bytes, tmp_path):
    from aspose.cells import Workbook
    p = tmp_path / "reopen.xlsx"
    p.write_bytes(data)
    return Workbook(str(p))


def _ws(wb, name):
    for w in wb.worksheets:
        if w.name == name:
            return w
    raise AssertionError(f"sheet {name!r} not in {[w.name for w in wb.worksheets]}")


def test_linked_render_snapshot_path(tmp_path):
    snap = _source_snapshot()
    links = _links()
    result = apply_links(_facts(), snap, links, skipped=[])
    assert len(result.filled) == 2
    data, stats, _a, _s, _c, linked = render_filled(
        _template(tmp_path), result.filled, _facts(),
        links=links, source_snapshot=snap, link_sources=True)
    assert linked is not None
    assert stats["linked"]["formula_cells"] == 2
    assert stats["linked"]["fallback_cells"] == 0
    assert stats["linked"]["verified"] is True
    assert stats["linked"]["sheets_added"] == ["Source - PL"]

    wb = _open(linked, tmp_path)
    names = [w.name for w in wb.worksheets]
    assert "Source - PL" in names
    src = _ws(wb, "Source - PL")
    assert src.cells.get("C5").value == 1000        # source data visible, values-only
    assert not src.cells.get("C5").is_formula
    pl = _ws(wb, "PL")
    c20, c21 = pl.cells.get("D20"), pl.cells.get("D21")
    assert c20.is_formula and "'Source - PL'" in c20.formula and "SUM" in c20.formula
    assert abs(float(c20.value) - 2.1) < 1e-9       # (1000+1100)*0.001
    assert c21.is_formula and c21.formula.startswith("=-(")
    assert abs(float(c21.value) - (-0.4)) < 1e-9
    # the VALUES deliverable is untouched by the linked pass
    wb_v = _open(data, tmp_path)
    assert not wb_v.worksheets[0].cells.get("D20").is_formula
    assert all(w.name != "Source - PL" for w in wb_v.worksheets)


def test_linked_render_bytes_path_copies_sheet_values_only(tmp_path):
    # a REAL source file whose cell holds a formula: the imported copy must be
    # frozen to its cached value, and the template formula must reference it.
    from aspose.cells import Workbook
    swb = Workbook()
    sws = swb.worksheets[0]
    sws.name = "PL"
    sws.cells.get("B5").put_value("Revenue")
    sws.cells.get("C5").formula = "=500*2"
    sws.cells.get("D5").put_value(1100)
    sws.cells.get("C6").put_value(400)
    swb.calculate_formula()
    sp = tmp_path / "source.xlsx"
    swb.save(str(sp))

    snap = _source_snapshot()
    links = _links()
    result = apply_links(_facts(), snap, links, skipped=[])
    _d, stats, _a, _s, _c, linked = render_filled(
        _template(tmp_path), result.filled, _facts(),
        links=links, source_snapshot=snap, source_path=sp, link_sources=True)
    assert linked is not None and stats["linked"]["formula_cells"] == 2
    wb = _open(linked, tmp_path)
    src = _ws(wb, "Source - PL")
    assert not src.cells.get("C5").is_formula       # frozen to value
    assert float(src.cells.get("C5").value) == 1000.0
    assert abs(float(_ws(wb, "PL").cells.get("D20").value) - 2.1) < 1e-9


def test_linked_mismatch_falls_back_to_raw_value(tmp_path):
    # a filled value that does NOT equal what its link's formula computes
    # (simulating any drift between apply and the formula layer) must revert
    # to the raw value — the two deliverables can never disagree.
    snap = _source_snapshot()
    links = _links()
    result = apply_links(_facts(), snap, links, skipped=[])
    tampered = [fc.model_copy(update={"value": 999.0}) if fc.template_cell == "D21" else fc
                for fc in result.filled]
    _d, stats, _a, _s, _c, linked = render_filled(
        _template(tmp_path), tampered, _facts(),
        links=links, source_snapshot=snap, link_sources=True)
    assert stats["linked"]["formula_cells"] == 1
    assert stats["linked"]["fallback_cells"] == 1
    assert stats["linked"]["mismatches"][0]["cell"] == "PL!D21"
    wb = _open(linked, tmp_path)
    cell = _ws(wb, "PL").cells.get("D21")
    assert not cell.is_formula
    assert float(cell.value) == 999.0


def test_link_sources_off_returns_no_linked_bytes(tmp_path):
    snap = _source_snapshot()
    links = _links()
    result = apply_links(_facts(), snap, links, skipped=[])
    _d, stats, _a, _s, _c, linked = render_filled(
        _template(tmp_path), result.filled, _facts(),
        links=links, source_snapshot=snap, link_sources=False)
    assert linked is None and "linked" not in stats


def test_linked_bytes_path_preserves_source_formatting(tmp_path):
    # the imported source sheet must keep fonts/bold/fills/column widths —
    # a cross-workbook Worksheet.copy() silently drops the style table, which
    # is why the import goes through Workbook.combine (a real regression).
    from aspose.cells import Workbook
    swb = Workbook()
    sws = swb.worksheets[0]
    sws.name = "PL"
    c = sws.cells.get("B5")
    c.put_value("Revenue")
    st = c.get_style()
    st.font.is_bold = True
    st.font.name = "Arial"
    st.font.size = 9
    c.set_style(st)
    sws.cells.get("C5").put_value(1000)
    sws.cells.get("D5").put_value(1100)
    sws.cells.get("C6").put_value(400)
    sws.cells.set_column_width(1, 37.5)
    sp = tmp_path / "styled-source.xlsx"
    swb.save(str(sp))

    snap = _source_snapshot()
    links = _links()
    result = apply_links(_facts(), snap, links, skipped=[])
    _d, stats, _a, _s, _c, linked = render_filled(
        _template(tmp_path), result.filled, _facts(),
        links=links, source_snapshot=snap, source_path=sp, link_sources=True)
    assert stats["linked"]["formula_cells"] == 2
    wb = _open(linked, tmp_path)
    src = _ws(wb, "Source - PL")
    st2 = src.cells.get("B5").get_style()
    assert st2.font.is_bold and st2.font.name == "Arial" and st2.font.size == 9
    assert abs(src.cells.get_column_width(1) - 37.5) < 1.0
