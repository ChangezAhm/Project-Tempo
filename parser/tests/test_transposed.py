"""Transposed-source support: a pack whose periods run DOWN rows and metrics
ACROSS columns must catalogue every series distinctly and emit real addresses
end-to-end (a real transposed pack once collapsed to one series per sheet and
filled 76 of 3,418 cells — silently)."""

from datetime import date

from app.population.apply import apply_links
from app.population.catalogue import (
    Series, catalogue_from_understanding, claim_orientation, series_cell,
)
from app.population.execute import execute_plan
from app.population.reconcile import reconcile_source_understanding
from app.population.schema import MetricMap
from app.population.units import Unit


# --- fixtures ----------------------------------------------------------------

def _snapshot():
    """Transposed sheet 'PL': dates in B5:B8 (down a column), series labels in
    G4:I4 (across a row), values in the grid G5:I8."""
    cells = [{"address": f"B{r}", "row": r, "col": 2,
              "value": f"2024-{r - 4:02d}-28"} for r in (5, 6, 7, 8)]
    for ci, (col, lab) in enumerate([(7, "Revenue"), (8, "COGS"), (9, "EBITDA")]):
        L = chr(ord("G") + ci)
        cells.append({"address": f"{L}4", "row": 4, "col": col, "value": lab})
        for r in (5, 6, 7, 8):
            cells.append({"address": f"{L}{r}", "row": r, "col": col,
                          "value": 100.0 * (ci + 1) + r})
    return {"sheets": [{"name": "PL", "cells": cells}]}


def _claims():
    return [{"sheet": "PL",
             "periods": [{"header_cell": f"B{r}", "date": f"2024-{r - 4:02d}-28",
                          "grain": "month", "kind": "actual"} for r in (5, 6, 7, 8)],
             "series": [{"label_cell": "G4", "label": "Revenue"},
                        {"label_cell": "H4", "label": "COGS"},
                        {"label_cell": "I4", "label": "EBITDA"}]}]


# --- orientation inference ---------------------------------------------------

def test_claim_orientation_classic_transposed_and_long_format():
    classic = claim_orientation(
        [{"header_cell": c} for c in ("C5", "D5", "E5", "F5")],
        [{"label_cell": c} for c in ("B7", "B8", "B9")])
    assert classic == "columns"
    transposed = claim_orientation(
        [{"header_cell": c} for c in ("B5", "B6", "B7", "B8")],
        [{"label_cell": c} for c in ("G4", "H4", "I4")])
    assert transposed == "rows"
    # long-format: periods AND series both vary by row — inconsistent
    long_fmt = claim_orientation(
        [{"header_cell": c} for c in ("B5", "B17", "B29")],
        [{"label_cell": c} for c in ("D5", "D6", "D7", "D8")])
    assert long_fmt == "unknown"


# --- catalogue ---------------------------------------------------------------

def test_transposed_catalogue_has_distinct_series_with_real_samples():
    cat = catalogue_from_understanding(_snapshot(), _claims())
    assert set(cat) == {"PL!c7", "PL!c8", "PL!c9"}       # one id per COLUMN
    rev = cat["PL!c7"]
    assert rev.transposed and rev.label == "Revenue"
    assert rev.sample[:2] == [105.0, 106.0]              # G5, G6 read correctly
    assert [a for (a, _d, _g) in rev.period_cols] == [5, 6, 7, 8]   # axis = ROWS
    assert series_cell(rev, 5) == "G5"                   # real address emitted


def test_long_format_sheet_is_flagged_not_collapsed():
    claims = [{"sheet": "PL",
               "periods": [{"header_cell": c, "date": None} for c in ("B5", "B17", "B29")],
               "series": [{"label_cell": f"D{r}", "label": f"K{r}"} for r in (5, 6, 7, 8)]}]
    diags: list[str] = []
    cat = catalogue_from_understanding(_snapshot(), claims, diagnostics=diags)
    assert cat == {}
    assert diags and "NOT catalogued" in diags[0]


# --- end-to-end: execute + apply on a transposed source ----------------------

def _facts():
    return [{"sheet_name": "T", "cell": "C7", "row": 7, "col": 3, "metric_label": "Revenue",
             "canonical_metric": None, "period_index": 0, "scenario": "actual"},
            {"sheet_name": "T", "cell": "D7", "row": 7, "col": 4, "metric_label": "Revenue",
             "canonical_metric": None, "period_index": 1, "scenario": "actual"}]


def test_execute_and_apply_read_the_transposed_grid():
    cat = catalogue_from_understanding(_snapshot(), _claims())
    maps = [MetricMap(metric="Revenue", series_id="PL!c7", confidence=0.9,
                      source_unit="GBP", target_unit="GBP")]
    ctx = ({}, {}, {("T", 3): date(2024, 1, 28), ("T", 4): date(2024, 2, 28)})
    demand = {"period_count": 2, "period_count_by_sheet": {"T": 2},
              "period_grain": "month", "metrics": [{"metric": "Revenue"}]}
    links, unmatched, _ = execute_plan(_facts(), cat, maps, demand, template_context=ctx)
    by_cell = {lk.template_cell: lk for lk in links}
    assert by_cell["C7"].source_cell == "G5"             # Jan-24 = row 5, col G
    assert by_cell["D7"].source_cell == "G6"
    result = apply_links(_facts(), _snapshot(), links, skipped=[])
    vals = {fc.template_cell: fc.value for fc in result.filled}
    assert vals == {"C7": 105.0, "D7": 106.0}            # the real grid values


# --- reconciler guards -------------------------------------------------------

def test_reconciler_skips_non_classic_sheets():
    sheets, report = reconcile_source_understanding(_snapshot(),
                                                    [dict(s) for s in _claims()])
    assert "non-classic orientation" in report["PL"]["skipped"]
    # nothing invented: the claim is untouched
    assert len(sheets[0]["periods"]) == 4 and len(sheets[0]["series"]) == 3


def test_reconciler_rejects_serial_range_timeline():
    # a 'monotonic' row of growing balances in Excel's serial range (29k..55k
    # ≈ 1979..2050) must never become a timeline (the BS_Quarterly incident)
    cells = [{"address": "A6", "row": 6, "col": 1, "value": "Balances"}]
    for i, v in enumerate([29000.0, 35000.0, 42000.0, 50000.0, 55000.0]):
        cells.append({"address": f"{chr(66 + i)}6", "row": 6, "col": 2 + i, "value": v})
        cells.append({"address": f"{chr(66 + i)}8", "row": 8, "col": 2 + i, "value": 1.0 + i})
    snap = {"sheets": [{"name": "BS", "cells": cells}]}
    claims = [{"sheet": "BS", "periods": [
        {"header_cell": c, "date": d, "grain": "quarter", "kind": "actual"}
        for c, d in (("B2", "2024-03-31"), ("C2", "2024-06-30"), ("D2", "2024-09-30"))],
        "series": [{"label_cell": "A8", "label": "Row8"}]}]
    _sheets, report = reconcile_source_understanding(snap, claims)
    assert report.get("BS", {}).get("period_cols_added", 0) == 0   # no 1979..2050 invention


def test_fiscal_start_inferred_from_fy_labels():
    from datetime import date as _d
    from app.population.catalogue import fiscal_start_month
    # April-start: FY26-Q3 ends Dec-2025; FY24-M10 ends Jan-2025
    ev = [("FY26-Q3", _d(2025, 12, 31)), ("FY26-Q2", _d(2025, 9, 30)),
          ("FY24-M10", _d(2025, 1, 31)), ("FY25-Q4", _d(2025, 3, 31))]
    assert fiscal_start_month(ev) == 4
    # calendar-fiscal: FY25-Q4 ends Dec-2025 -> start January
    cal = [("FY25-Q1", _d(2025, 3, 31)), ("FY25-Q2", _d(2025, 6, 30)),
           ("FY25-Q4", _d(2025, 12, 31))]
    assert fiscal_start_month(cal) == 1
    # no FY labels / too few -> None
    assert fiscal_start_month([("Jan-25", _d(2025, 1, 31))]) is None
    assert fiscal_start_month([("FY26-Q3", _d(2025, 12, 31))]) is None


def test_fy_slot_from_fiscal_source_is_flagged():
    from datetime import date as _d
    cat = {"BS!c5": Series(
        id="BS!c5", sheet="BS", row=5, label="Fixed assets",
        period_cols=[(9, _d(2025, 3, 31), "quarter"), (10, _d(2025, 6, 30), "quarter"),
                     (11, _d(2025, 9, 30), "quarter"), (12, _d(2025, 12, 31), "quarter")],
        unit=Unit(1.0, "GBP", "money"), sample=[10.0],
        col_scenario={9: "actual", 10: "actual", 11: "actual", 12: "actual"},
        transposed=True, fiscal_start=4)}
    facts = [{"sheet_name": "T", "cell": "AZ7", "row": 7, "col": 52,
              "metric_label": "Fixed assets", "canonical_metric": None,
              "period_index": 0, "scenario": "actual",
              "period_type": "year", "parsed_date": "2025"}]
    maps = [MetricMap(metric="Fixed assets", series_id="BS!c5", confidence=0.9,
                      source_unit="GBP", target_unit="GBP", rollup="end")]
    demand = {"period_count": 1, "period_count_by_sheet": {"T": 1},
              "period_grain": "month", "metrics": [{"metric": "Fixed assets"}]}
    links, _u, _ = execute_plan(facts, cat, maps, demand, template_context=({}, {}, {}))
    assert links and links[0].source_cell == "E12"       # transposed Q4 balance
    assert "fiscal:" in (links[0].note or "")            # convention flagged


def test_sheet_grid_renders_values_percent_and_row_summary():
    from app.population.grids import sheet_grid, workbook_grids
    sheet = {"name": "PL", "cells": [
        {"address": "B4", "row": 4, "col": 2, "value": "Revenue"},
        {"address": "C4", "row": 4, "col": 3, "value": 1234.5},
        {"address": "D4", "row": 4, "col": 4, "value": 0.42,
         "style": {"number_format": "0.0%"}},
        {"address": "E4", "row": 4, "col": 5, "value": "=CX.GET(x)"},   # formula text: excluded
    ]}
    g = sheet_grid(sheet)
    assert "B4='Revenue'" in g and "C4=1234.5" in g and "D4=0.42%" in g
    assert "CX.GET" not in g
    # oversized sheet degrades to bannered row summary, and the assembler reports it
    big = {"name": "Huge", "cells": [
        {"address": f"C{r}", "row": r, "col": 3, "value": float(r)} for r in range(1, 40)]}
    small_cap = sheet_grid(big, char_cap=100)
    assert "[too large" in small_cap.splitlines()[0]
    block, notes = workbook_grids({"sheets": [sheet]}, ["PL"], title="SRC")
    assert block.startswith("### SRC ###") and notes == []


def test_grid_mode_single_call_carries_both_grids(monkeypatch):
    import app.population.mapping as mapping
    calls = []

    def fake_stream(**kw):
        calls.append(kw)
        return None, ('{"mappings":[{"metric":"Revenue","series_id":"PL!c7",'
                      '"status":"direct","confidence":0.9}]}')

    monkeypatch.setattr(mapping, "guarded_stream", fake_stream)
    cat = catalogue_from_understanding(_snapshot(), _claims())
    metrics = [{"metric": "Revenue", "label": "Revenue"}]
    maps, failed = mapping.map_metrics(metrics, cat, grids="### GRIDS ###\nr4: G4='Revenue'")
    assert not failed and maps[0].series_id == "PL!c7"
    assert len(calls) == 1                                # single call, no batching
    assert calls[0]["model"] == mapping.MODEL_SMART       # strong model in grid mode
    stable = calls[0]["content"][0]["text"]               # cached prefix block
    assert "### GRIDS ###" in stable and "You can SEE both workbooks" in stable
    assert calls[0]["cache_blocks"] == 1
