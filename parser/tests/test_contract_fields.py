"""Offline tests for the Template Contract field aggregation (no Supabase —
`_aggregate_fields` is the pure core that `get_contract_fields` wraps)."""

from app.datamodel.persist import _aggregate_fields, _modal


def _fact(**kw):
    base = {"sheet_name": "P&L", "cell": "E8", "row": 8, "col": 5,
            "metric_label": "Revenue", "canonical_metric": "revenue",
            "scenario": "actual", "category": "data", "unit": "GBP '000",
            "sign_convention": None, "applied_correction_ids": []}
    base.update(kw)
    return base


def _cell(col: int, row: int, **kw):
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return _fact(cell=f"{letters[col - 1]}{row}", row=row, col=col, **kw)


# --- modal helper -----------------------------------------------------------

def test_modal_most_common_non_empty():
    assert _modal(["a", None, "b", "b", ""]) == "b"


def test_modal_tie_broken_by_first_occurrence():
    assert _modal(["x", "y", "y", "x"]) == "x"


def test_modal_all_empty_is_none():
    assert _modal([None, "", None]) is None


# --- grouping ---------------------------------------------------------------

def test_groups_by_sheet_and_metric_label():
    facts = [
        _cell(5, 8), _cell(6, 8),                              # P&L / Revenue
        _cell(5, 9, metric_label="COGS", canonical_metric="cogs"),
        _cell(5, 8, sheet_name="BS", metric_label="Cash", canonical_metric="cash"),
    ]
    fields = _aggregate_fields(facts)
    assert [(f["sheet_name"], f["metric_label"]) for f in fields] == [
        ("BS", "Cash"), ("P&L", "Revenue"), ("P&L", "COGS")]
    assert fields[1]["fact_count"] == 2


def test_fields_sorted_by_sheet_then_first_row_col():
    facts = [
        _cell(2, 20, metric_label="Later"),
        _cell(9, 3, metric_label="Earlier"),
        _cell(3, 3, metric_label="Earliest"),   # same row as "Earlier", smaller col
    ]
    fields = _aggregate_fields(facts)
    assert [f["metric_label"] for f in fields] == ["Earliest", "Earlier", "Later"]


def test_unlabelled_fallback():
    fields = _aggregate_fields([_fact(metric_label=None), _fact(metric_label="", col=6, cell="F8")])
    assert len(fields) == 1
    assert fields[0]["metric_label"] == "(unlabelled)"
    assert fields[0]["fact_count"] == 2


# --- per-field aggregates ---------------------------------------------------

def test_modal_dimensions_and_scenarios():
    facts = [
        _cell(5, 8, canonical_metric=None, unit="GBP", scenario="actual"),
        _cell(6, 8, canonical_metric="revenue", unit="USD", scenario="budget"),
        _cell(7, 8, canonical_metric="revenue", unit="GBP", scenario="actual"),
    ]
    (f,) = _aggregate_fields(facts)
    assert f["canonical_metric"] == "revenue"       # non-null wins over None
    assert f["unit"] == "GBP"                       # 2 vs 1
    assert f["sign_convention"] is None
    assert f["scenarios"] == ["actual", "budget"]   # distinct, sorted


def test_category_counts_only_nonzero_and_fillable():
    facts = [
        _cell(5, 8, category="data"), _cell(6, 8, category="data"),
        _cell(7, 8, category="sourced"), _cell(8, 8, category="computed"),
    ]
    (f,) = _aggregate_fields(facts)
    assert f["category_counts"] == {"data": 2, "sourced": 1, "computed": 1}  # no "exclude" key
    assert f["fillable_count"] == 3                 # data + sourced
    assert f["fact_count"] == 4


def test_cells_first_three_by_row_col():
    facts = [_cell(7, 8), _cell(5, 9), _cell(6, 8), _cell(5, 8)]
    (f,) = _aggregate_fields(facts)
    assert f["cells"] == ["E8", "F8", "G8"]         # (row, col) order, capped at 3


def test_corrected_flag():
    clean = _aggregate_fields([_cell(5, 8)])
    fixed = _aggregate_fields([_cell(5, 8), _cell(6, 8, applied_correction_ids=["c1"])])
    assert clean[0]["corrected"] is False
    assert fixed[0]["corrected"] is True


def test_empty_facts():
    assert _aggregate_fields([]) == []
