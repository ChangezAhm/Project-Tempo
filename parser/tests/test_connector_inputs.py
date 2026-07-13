"""Connector-fed cells as replaceable inputs (the 'a connector cell holding a
company financial figure IS an input' fix). Covers the pure classification seam,
the numeric/date-format precision filters, the CX field-name relabelling, the
demand row-N gate, and the critical clear-only-what-you-fill safety."""

import math

from app.datamodel.derive import (
    _classify_category,
    _connector_field,
    _cx_args,
    _cx_identity,
    _is_date_format,
    _numeric,
)


# --- _numeric: a finite-number sniff test ------------------------------------
def test_numeric_accepts_figures():
    for v in (95.76, 0, -3, "65.71", "1,200", "68.6%", "(1,200)", "€5", "$1,200", "1e6", "2025"):
        assert _numeric(v), v


def test_numeric_rejects_status_and_nonfinite():
    for v in ("GREEN", "n/a", "", "   ", None, True, False, "inf", "nan", "1.2.3", "-", "%"):
        assert not _numeric(v), v
    assert not _numeric(float("nan")) and not _numeric(float("inf"))   # bool guard + isfinite


# --- _is_date_format: keep date serials/years out ----------------------------
def test_is_date_format():
    for fmt in ("yyyy-mm-dd", "mmm-yy", "dd/mm/yyyy", "[$-409]d-mmm-yy", "m/d/yy"):
        assert _is_date_format(fmt), fmt
    for fmt in ("#,##0.0", "0.0%", "_(* #,##0.0_)", "General", None, ""):
        assert not _is_date_format(fmt), fmt


# --- _connector_field: the CX_GET literal field name -------------------------
def test_connector_field_extraction():
    f = '=IF("CX.UNLINK"="CX.UNLINK",95.7,IFERROR(CX_GET(CX_INV_ID,"Cash returned LCY [Inv]",D),0))'
    assert _connector_field(f) == "Cash returned LCY [Inv]"
    assert _connector_field('=CX_GET(1,$A23)') is None          # cell-ref arg, not a literal
    assert _connector_field("=SUM(A1:A9)") is None              # not a connector


# --- _cx_args / _cx_identity: the self-describing connector grid --------------
def test_cx_args_respects_quotes_sheet_names_and_nesting():
    f = '=IF("CX.UNLINK"="CX.UNLINK",95.76,IFERROR(_xldudf_CX_GET(CX_ENTITY,' \
        "'Flash Rolling Monthly'!G$5,$F25,\"Month\",,0),0))"
    assert _cx_args(f) == ["CX_ENTITY", "'Flash Rolling Monthly'!G$5", "$F25", '"Month"', "", "0"]
    assert _cx_args("=SUM(A1:A2)") == []


def test_cx_identity_resolves_transposed_grid():
    # arg2 (G5) -> metric header; arg3 (F25) -> period-axis date; arg4 -> grain.
    f = '=IF("CX.UNLINK"="CX.UNLINK",95.76,IFERROR(_xldudf_CX_GET(CX_ENTITY,' \
        "'Flash Rolling Monthly'!G$5,$F25,\"Month\",,0),0))"
    cell_val = {("Flash Rolling Monthly", 5, 7): "Net Revenue",
                ("Flash Rolling Monthly", 25, 6): "2023-11-30T00:00:00"}
    assert _cx_identity(f, "Flash Rolling Monthly", cell_val, {}) == ("Net Revenue", "2023-11", "monthly")


def test_cx_identity_literal_metric_no_period():
    # a summary cell: literal metric name, no resolvable period/grain.
    f = '=IFERROR(CX_GET(CX_INV_ID,"Cash returned LCY [Inv]",X),0)'
    assert _cx_identity(f, "S", {}, {}) == ("Cash returned LCY [Inv]", None, None)


def test_cx_identity_bails_on_multiple_cx_get():
    # two CX_GET → ambiguous arg positions (leftmost may be a config wrapper) →
    # no guessed identity, fall back to the ordinary detector.
    f = '=DATE(YEAR(CX_GET(E,"fiscal_year_end")),CX_GET(E,"Revenue",D))'
    assert _cx_identity(f, "S", {}, {}) == (None, None, None)


def test_cx_args_keeps_trailing_arg_on_missing_close_paren():
    # a truncated formula must not silently drop its last argument
    assert _cx_args('=CX_GET(E,"Rev",D') == ["E", '"Rev"', "D"]


# --- _classify_category: the full cascade, table-driven ----------------------
def test_classify_matrix():
    S = set()
    # connector financial cell → sourced (overrides calc role — an input however fed)
    assert _classify_category("Revenue", '=CX_GET(1,"Revenue")', None, "calc", "Sh", S) == ("sourced", None)
    # connector cell with a CONTROL label → config, NOT sourced
    assert _classify_category("Selected Company", '=CX_GET(1,"x")', None, None, "Sh", S) == ("config", "control")
    # GUARD: a control-ish label on a genuine (non-connector) FORMULA stays computed
    assert _classify_category("override flags", "=SUM(A1:A2)", None, None, "Sh", S) == ("computed", None)
    # control / placeholder literals → config
    assert _classify_category("POC Mode", "", None, None, "Sh", S) == ("config", "control")
    assert _classify_category("KPI Label 1", "", None, None, "Sh", S) == ("config", "placeholder")
    # ordinary cascade unchanged
    assert _classify_category("Revenue", "", "exclude", None, "Sh", S) == ("exclude", None)
    assert _classify_category("Gross Profit", "=A1-A2", None, None, "Sh", S) == ("computed", None)
    assert _classify_category("Buffer", "", None, "calc", "Sh", S) == ("staging", None)
    assert _classify_category("Buffer", "", None, "calc", "Sh", {"Sh"}) == ("data", None)   # input surface wins
    assert _classify_category("Revenue", "", None, "input", "Sh", S) == ("data", None)


# --- demand gate: 'row N' fallbacks never generate mapping demand ------------
def test_build_demand_drops_rown_labels(monkeypatch):
    from app.datamodel.derive import DERIVATION_VERSION
    from app.population import run as R
    facts = [
        {"sheet_name": "F", "cell": "C25", "row": 25, "col": 3, "canonical_metric": None,
         "metric_label": "row 25", "category": "sourced", "value_role": None,
         "period_index": None, "period_type": None, "scenario": "unknown", "unit": None},
        {"sheet_name": "F", "cell": "C8", "row": 8, "col": 3, "canonical_metric": None,
         "metric_label": "Cash returned LCY [Inv]", "category": "sourced", "value_role": None,
         "period_index": 0, "period_type": "monthly", "scenario": "unknown", "unit": None},
    ]
    monkeypatch.setattr(R, "get_data_model", lambda tid, limit=30000: {
        "available": True, "facts": facts,
        "model": {"dimensions": {"derivation_version": DERIVATION_VERSION}, "period_grains": ["monthly"]}})
    demand, inputs = R.build_demand("t", None)
    keys = {m["metric"] for m in demand["metrics"]}
    assert keys == {"Cash returned LCY [Inv]"}          # 'row 25' dropped from demand
    assert any(f["metric_label"] == "row 25" for f in inputs)   # but still a fact in the model


# --- CRITICAL: a sourced connector cell is not wiped unless refilled ----------
class _FC:
    def __init__(self, sheet, cell, value):
        self.template_sheet, self.template_cell, self.value = sheet, cell, value


def _connector_template(tmp_path):
    from aspose.cells import Workbook
    wb = Workbook(); ws = wb.worksheets[0]; ws.name = "S"
    ws.cells.get("A2").put_value("Revenue")
    ws.cells.get("B2").put_value(8.2)            # a stale MANUAL literal (data)
    ws.cells.get("B3").formula = "=100*2"        # a connector-style formula cell (sourced)
    ws.cells.get("B4").put_value(95.76)          # a value-SAVED connector cell (sourced literal)
    wb.calculate_formula()
    p = tmp_path / "t.xlsx"; wb.save(str(p))
    return p


def _open(data, tmp_path):
    from aspose.cells import Workbook
    p = tmp_path / "out.xlsx"; p.write_bytes(data)
    return Workbook(str(p)).worksheets[0]


def test_unmatched_sourced_cell_is_never_cleared(tmp_path):
    from app.population.run import render_filled
    clear_facts = [
        {"sheet_name": "S", "cell": "B2", "category": "data"},      # manual input → stale-wipe OK
        {"sheet_name": "S", "cell": "B3", "category": "sourced"},   # connector formula, unmatched
        {"sheet_name": "S", "cell": "B4", "category": "sourced"},   # value-saved connector, unmatched
    ]
    data, stats, _a, _s, _c = render_filled(
        _connector_template(tmp_path), filled=[], clear_facts=clear_facts, reset="full")
    ws = _open(data, tmp_path)
    assert ws.cells.get("B2").value is None                 # manual stale value wiped
    assert ws.cells.get("B3").is_formula                    # connector FORMULA preserved (not blanked)
    assert ws.cells.get("B4").value == 95.76                # value-saved connector preserved


def test_matched_sourced_cell_is_cleared_and_filled(tmp_path):
    from app.population.run import render_filled
    clear_facts = [{"sheet_name": "S", "cell": "B4", "category": "sourced"}]
    filled = [_FC("S", "B4", 1234.0)]                       # a source value maps here
    data, _stats, _a, _s, _c = render_filled(
        _connector_template(tmp_path), filled=filled, clear_facts=clear_facts, reset="full")
    ws = _open(data, tmp_path)
    assert ws.cells.get("B4").value == 1234.0               # replaced by the uploaded figure
