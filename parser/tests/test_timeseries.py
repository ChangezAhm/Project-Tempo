"""The data-model time-series projection: metrics x periods per tab, with a
scenario dimension. Pure reshape over facts — get_data_model is stubbed."""

from app.datamodel import persist


def _facts():
    return [
        {"sheet_name": "P&L", "metric_label": "Revenue", "canonical_metric": "revenue",
         "unit": "€'000", "category": "data", "row": 5, "col": 3, "period_index": 0,
         "parsed_date": "2026-01-31", "period_label": "Jan-26", "period_type": "month",
         "scenario": "unknown", "cell": "C5", "basis": "flow", "definition": "monthly revenue"},
        {"sheet_name": "P&L", "metric_label": "Revenue", "canonical_metric": "revenue",
         "unit": "€'000", "category": "data", "row": 5, "col": 4, "period_index": 1,
         "parsed_date": "2026-02-28", "period_label": "Feb-26", "period_type": "month",
         "scenario": "budget", "cell": "D5", "basis": "flow", "definition": None},
        {"sheet_name": "P&L", "metric_label": "Gross Profit", "canonical_metric": "gross_profit",
         "unit": "€'000", "category": "computed", "row": 6, "col": 3, "period_index": 0,
         "parsed_date": "2026-01-31", "period_label": "Jan-26", "period_type": "month",
         "scenario": "unknown", "cell": "C6", "basis": "flow", "definition": None},
        {"sheet_name": "Control", "metric_label": "As-of date:", "canonical_metric": None,
         "unit": None, "category": "data", "row": 2, "col": 2, "period_index": None,
         "parsed_date": None, "period_label": None, "period_type": "other",
         "scenario": "unknown", "cell": "B2", "basis": None, "definition": None},
        # excluded category must not appear
        {"sheet_name": "Cover", "metric_label": "Instructions", "canonical_metric": None,
         "unit": None, "category": "exclude", "row": 1, "col": 1, "period_index": None,
         "parsed_date": None, "period_label": None, "period_type": "other",
         "scenario": "unknown", "cell": "A1", "basis": None, "definition": None},
    ]


def test_timeseries_projection(monkeypatch):
    monkeypatch.setattr(persist, "get_data_model",
                        lambda tid, limit=30000: {"available": True, "template_version_id": "v1", "facts": _facts()})
    out = persist.timeseries_view("t1")
    assert out["available"] is True
    # unknown -> actual; budget stays budget
    assert set(out["scenarios"]) == {"actual", "budget"}

    sheets = {s["sheet"]: s for s in out["sheets"]}
    assert set(sheets) == {"P&L", "Control"}          # 'exclude' category dropped whole Cover tab

    pl = sheets["P&L"]
    assert pl["is_timeseries"] is True and len(pl["periods"]) == 2
    # metrics ordered by row: Revenue (5) before Gross Profit (6)
    assert [m["label"] for m in pl["metrics"]] == ["Revenue", "Gross Profit"]
    rev = pl["metrics"][0]
    assert rev["cells"]["actual"]["2026-01-31"] == "C5"   # unlabelled slot -> actual
    assert rev["cells"]["budget"]["2026-02-28"] == "D5"
    assert pl["metrics"][1]["category"] == "computed"

    # single-period config tab is flagged and sorted last
    assert out["sheets"][-1]["sheet"] == "Control"
    assert out["sheets"][-1]["is_timeseries"] is False


def test_header_date_helpers():
    # Relative-timeline month headers are formulas whose date is in cached_value —
    # read as a datetime, an ISO string, or an Excel serial.
    from datetime import date, datetime

    from app.datamodel.derive import _grain_from_dates, _iso_label, _parse_header_date
    assert _parse_header_date("2025-01-31T00:00:00") == "2025-01"
    assert _parse_header_date(datetime(2025, 3, 31)) == "2025-03"
    assert _parse_header_date(date(2025, 6, 30)) == "2025-06"
    assert _parse_header_date(45658) == "2025-01"          # Excel serial for 2025-01-01
    assert _parse_header_date("Recurring Revenue") is None
    assert _parse_header_date(None) is None and _parse_header_date(True) is None

    assert _iso_label("2025-01") == "Jan-25"
    # grain inferred from date SPACING (this is what overrides the LLM's 'annual')
    assert _grain_from_dates(["2025-01", "2025-02", "2025-03"]) == "monthly"
    assert _grain_from_dates(["2024-09", "2024-12", "2025-03"]) == "quarterly"
    assert _grain_from_dates(["2025-01"]) is None           # one date -> unknown


def test_timeseries_unavailable(monkeypatch):
    monkeypatch.setattr(persist, "get_data_model",
                        lambda tid, limit=30000: {"available": False, "template_version_id": "v1"})
    out = persist.timeseries_view("t1")
    assert out["available"] is False and out["sheets"] == []
