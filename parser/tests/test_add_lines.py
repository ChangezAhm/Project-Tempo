"""Add-line-item engine (authoring.py): propose_additions is pure (no Aspose),
apply_additions is exercised against a tiny duck-typed fake worksheet matching
the narrow surface documented in its docstring."""

from datetime import date

from app.population.authoring import apply_additions, propose_additions
from app.population.catalogue import Series
from app.population.units import Unit

# ---------------------------------------------------------------- helpers

MONEY_USD = Unit(base=1.0, currency="USD", kind="money")
PCT = Unit(base=1.0, currency=None, kind="percent")


def _series(sid, sheet, row, label, cols_dates, unit=MONEY_USD):
    """cols_dates: [(col_index, date)] -> monthly period_cols."""
    return Series(id=sid, sheet=sheet, row=row, label=label,
                  period_cols=[(c, d, "month") for c, d in cols_dates],
                  unit=unit, sample=[1.0])


def _region(**over):
    base = {
        "sheet_name": "KPIs", "kind": "kpi_list", "label_col": 2,
        "value_cols": [{"col": 3, "parsed_date": "2024-01"},
                       {"col": 4, "parsed_date": "2024-02"}],
        "row_start": 10, "row_end": 13, "total_row": None,
        "rules": {"unit": "any"}, "confidence": 0.9,
    }
    base.update(over)
    return base


def _cat(*series):
    return {s.id: s for s in series}


# ------------------------------------------------- propose_additions (pure)

def test_unused_series_with_date_overlap_is_proposed_with_correct_source_cells():
    # Source col 28 = AB (two-letter col — the classic address bug), row 7.
    s = _series("Src!r7", "Src", 7, "Churn",
                [(28, date(2024, 1, 31)), (29, date(2024, 2, 29))])
    props, notes = propose_additions(_cat(s), set(), [_region()])
    assert notes == []
    assert len(props) == 1
    p = props[0]
    assert p["sheet_name"] == "KPIs" and p["row"] == 10 and p["label_col"] == 2
    assert p["label"] == "Churn" and p["kind"] == "kpi_list"
    assert p["source_series_id"] == "Src!r7"
    assert p["unit"] == "money USD"
    assert p["region_rules"] == {"unit": "any"} and p["confidence"] == 0.9
    # 31-Jan source matches the region's '2024-01' column (monthly bucket),
    # and the cited address uses correct multi-letter columns.
    assert p["values"] == [
        {"col": 3, "source_sheet": "Src", "source_cell": "AB7"},
        {"col": 4, "source_sheet": "Src", "source_cell": "AC7"},
    ]


def test_used_series_are_excluded():
    s = _series("Src!r7", "Src", 7, "Revenue", [(3, date(2024, 1, 31))])
    props, notes = propose_additions(_cat(s), {"Src!r7"}, [_region()])
    assert props == [] and notes == []


def test_no_date_overlap_means_no_proposal():
    # Series only has 2023 months; the region's columns are 2024.
    s = _series("Src!r7", "Src", 7, "Churn", [(3, date(2023, 1, 31))])
    props, _ = propose_additions(_cat(s), set(), [_region()])
    assert props == []


def test_dateless_value_col_matches_nothing():
    # parsed_date=None is conservative: with no date we'd be guessing alignment.
    s = _series("Src!r7", "Src", 7, "Churn", [(3, date(2024, 1, 31))])
    region = _region(value_cols=[{"col": 3, "parsed_date": None}])
    props, _ = propose_additions(_cat(s), set(), [region])
    assert props == []


def test_partial_overlap_only_cites_the_matching_columns():
    # Series has Jan only -> the Feb region column gets no value entry.
    s = _series("Src!r7", "Src", 7, "Churn", [(5, date(2024, 1, 1))])
    props, _ = propose_additions(_cat(s), set(), [_region()])
    assert props[0]["values"] == [{"col": 3, "source_sheet": "Src", "source_cell": "E7"}]


def test_capacity_and_overflow_note():
    # 2 free rows, 3 fitting candidates -> 2 proposals + an overflow note.
    ss = [_series(f"Src!r{r}", "Src", r, f"KPI {r}", [(3, date(2024, 1, 31))])
          for r in (5, 6, 7)]
    region = _region(row_start=10, row_end=11)
    props, notes = propose_additions(_cat(*ss), set(), [region])
    assert [p["row"] for p in props] == [10, 11]
    assert notes == ["region KPIs!r10-r11 full: 1 candidates skipped"]


def test_total_row_is_never_proposed_into():
    # Rows 10..12 with total_row=12: capacity is 2 and row 12 is never assigned.
    ss = [_series(f"Src!r{r}", "Src", r, f"KPI {r}", [(3, date(2024, 1, 31))])
          for r in (5, 6, 7)]
    region = _region(row_start=10, row_end=12, total_row=12)
    props, notes = propose_additions(_cat(*ss), set(), [region])
    assert [p["row"] for p in props] == [10, 11]
    assert 12 not in {p["row"] for p in props}
    assert notes and "1 candidates skipped" in notes[0]


def test_max_per_region_caps_proposals():
    ss = [_series(f"Src!r{r}", "Src", r, f"KPI {r}", [(3, date(2024, 1, 31))])
          for r in (5, 6, 7)]
    props, notes = propose_additions(_cat(*ss), set(), [_region()], max_per_region=1)
    assert len(props) == 1 and props[0]["row"] == 10
    assert notes == ["region KPIs!r10-r13 full: 2 candidates skipped"]


def test_series_is_placed_once_across_regions_and_assignment_is_top_down():
    # Two regions; the single candidate lands in the FIRST fitting region only —
    # two proposals for one series would double-write the same data.
    s = _series("Src!r7", "Src", 7, "Churn", [(3, date(2024, 1, 31))])
    r1 = _region(sheet_name="KPIs")
    r2 = _region(sheet_name="Other", row_start=20, row_end=22)
    props, _ = propose_additions(_cat(s), set(), [r1, r2])
    assert len(props) == 1 and props[0]["sheet_name"] == "KPIs"


def test_percent_series_is_proposed_but_unit_is_visible_to_reviewer():
    # Kept simple by design: no unit filtering — the reviewer sees 'percent'.
    s = _series("Src!r7", "Src", 7, "Margin %", [(3, date(2024, 1, 31))], unit=PCT)
    props, _ = propose_additions(_cat(s), set(), [_region()])
    assert props[0]["unit"] == "percent"


def test_proposals_are_deterministic_ordering_by_sheet_then_row():
    a = _series("Z!r9", "Z", 9, "Zed", [(3, date(2024, 1, 31))])
    b = _series("A!r2", "A", 2, "Ay", [(3, date(2024, 1, 31))])
    props1, _ = propose_additions({a.id: a, b.id: b}, set(), [_region()])
    props2, _ = propose_additions({b.id: b, a.id: a}, set(), [_region()])
    assert props1 == props2
    assert [p["source_series_id"] for p in props1] == ["A!r2", "Z!r9"]


# ------------------------------------------- apply_additions (fake workbook)

class FakeCell:
    def __init__(self, ws, addr):
        self.ws, self.addr = ws, addr
        self.value = None
        self.style = f"style@{addr}"      # each cell has a distinct native style

    def put_value(self, v):
        self.value = v

    def get_style(self):
        if self.ws.style_raises:
            raise RuntimeError("style unavailable")
        return self.style

    def set_style(self, style):
        if self.ws.style_raises:
            raise RuntimeError("style unavailable")
        self.ws.styles_set[self.addr] = style


class FakeCells:
    def __init__(self, ws):
        self.ws = ws
        self._cells = {}

    def get(self, addr):
        return self._cells.setdefault(addr, FakeCell(self.ws, addr))


class FakeWs:
    """The narrow surface apply_additions documents: cells.get(a1) -> cell with
    .value / .put_value / .get_style / .set_style."""

    def __init__(self, style_raises=False):
        self.style_raises = style_raises
        self.styles_set = {}
        self.cells = FakeCells(self)


def _proposal(**over):
    base = {
        "sheet_name": "KPIs", "row": 10, "label_col": 2, "label": "Churn",
        "kind": "kpi_list", "source_series_id": "Src!r7", "unit": "money USD",
        "values": [{"col": 3, "source_sheet": "Src", "source_cell": "AB7"},
                   {"col": 4, "source_sheet": "Src", "source_cell": "AC7"}],
        "region_rules": None, "confidence": 0.9,
        "total_row": 13, "row_start": 10,
    }
    base.update(over)
    return base


def test_apply_writes_label_and_numeric_values():
    ws = FakeWs()
    sval = {("Src", "AB7"): 12.5, ("Src", "AC7"): "1300"}   # numeric string coerces
    applied, skipped = apply_additions({"KPIs": ws}, [_proposal()], sval)
    assert skipped == []
    assert applied == [{"sheet_name": "KPIs", "row": 10, "label": "Churn",
                        "cells_written": 2, "source_series_id": "Src!r7"}]
    assert ws.cells.get("B10").value == "Churn"
    assert ws.cells.get("C10").value == 12.5
    assert ws.cells.get("D10").value == 1300.0


def test_apply_skips_empty_formula_error_and_text_source_values():
    ws = FakeWs()
    sval = {("Src", "AB7"): "=SUM(A1:A3)", ("Src", "AC7"): "#REF!"}
    applied, _ = apply_additions({"KPIs": ws}, [_proposal()], sval)
    assert applied[0]["cells_written"] == 0          # label written, no values
    assert ws.cells.get("B10").value == "Churn"
    assert ws.cells.get("C10").value is None and ws.cells.get("D10").value is None

    ws2 = FakeWs()
    sval2 = {("Src", "AB7"): None, ("Src", "AC7"): "n/a"}   # absent + noise text
    applied2, _ = apply_additions({"KPIs": ws2}, [_proposal()], sval2)
    assert applied2[0]["cells_written"] == 0


def test_apply_refuses_occupied_label_cell():
    # The live workbook has something at B10 (stale region map / fixed label):
    # the proposal must be skipped with a reason, nothing written.
    ws = FakeWs()
    ws.cells.get("B10").put_value("Revenue")
    applied, skipped = apply_additions({"KPIs": ws}, [_proposal()],
                                       {("Src", "AB7"): 1.0})
    assert applied == []
    assert len(skipped) == 1 and "not empty" in skipped[0]["reason"]
    assert ws.cells.get("B10").value == "Revenue"    # untouched
    assert ws.cells.get("C10").value is None


def test_apply_never_writes_the_total_row():
    ws = FakeWs()
    p = _proposal(row=13)   # row == total_row: must be assert-skipped
    applied, skipped = apply_additions({"KPIs": ws}, [p], {("Src", "AB7"): 1.0})
    assert applied == []
    assert len(skipped) == 1 and "total row" in skipped[0]["reason"]
    assert ws.cells.get("B13").value is None


def test_apply_skips_missing_sheet_with_reason():
    applied, skipped = apply_additions({}, [_proposal()], {})
    assert applied == []
    assert skipped[0]["reason"] == "sheet not found in workbook"
    assert skipped[0]["source_series_id"] == "Src!r7"


def test_apply_copies_sibling_style_from_row_above_row_start():
    ws = FakeWs()
    applied, _ = apply_additions({"KPIs": ws}, [_proposal()], {("Src", "AB7"): 5.0})
    assert applied[0]["cells_written"] == 1
    # style source is row_start-1 = row 9, same column, for label and value cells
    assert ws.styles_set["B10"] == "style@B9"
    assert ws.styles_set["C10"] == "style@C9"


def test_style_failure_never_loses_the_written_value():
    ws = FakeWs(style_raises=True)
    applied, skipped = apply_additions({"KPIs": ws}, [_proposal()],
                                       {("Src", "AB7"): 5.0})
    assert skipped == []
    assert applied[0]["cells_written"] == 1
    assert ws.cells.get("B10").value == "Churn" and ws.cells.get("C10").value == 5.0
    assert ws.styles_set == {}   # styling failed silently, values survived
