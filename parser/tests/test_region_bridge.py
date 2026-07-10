"""The mapping↔authoring bridge: used-series completeness (the double-write
regression), region activation on unavailable hosted metrics, candidate ranking,
and the review-item shapes. Pure — no LLM, no Supabase."""

from datetime import date

from app.population.catalogue import Series
from app.population.region_bridge import (
    addition_review_items,
    rank_candidates,
    region_hosted_metrics,
    route_additions,
    used_series,
)
from app.population.schema import MetricMap
from app.population.units import Unit

MONEY = Unit(base=1.0, currency="USD", kind="money")
PCT = Unit(base=1.0, currency=None, kind="percent")


def _series(sid, row, label, unit=MONEY):
    return Series(id=sid, sheet="Src", row=row, label=label,
                  period_cols=[(3, date(2026, 1, 31), "month")], unit=unit, sample=[1.0])


def test_used_series_includes_also_series_ids():
    # THE regression: a series consumed only inside an aggregate must count as
    # used, or it gets re-proposed as a new line -> double-write.
    maps = [MetricMap(metric="opex", series_id="Src!r5", also_series_ids=["Src!r6", "Src!r7"])]
    assert used_series(maps) == {"Src!r5", "Src!r6", "Src!r7"}


def test_region_hosted_metrics_maps_facts_into_slot_rows():
    regions = [{"sheet_name": "KPIs", "row_start": 10, "row_end": 12, "label_col": 2,
                "slots": [{"row": 10, "mode": "blank"}, {"row": 11, "mode": "placeholder"}]}]
    facts = [
        {"sheet_name": "KPIs", "row": 11, "canonical_metric": None, "metric_label": "Custom KPI grid"},
        {"sheet_name": "KPIs", "row": 40, "canonical_metric": "revenue", "metric_label": "Revenue"},
        {"sheet_name": "P&L", "row": 11, "canonical_metric": "cogs", "metric_label": "COGS"},
    ]
    hosted = region_hosted_metrics(facts, regions)
    assert hosted == {0: {"Custom KPI grid"}}    # right sheet + slot row only


def test_unavailable_hosted_metric_activates_region_with_ranked_candidates():
    regions = [{"sheet_name": "KPIs", "kind": "kpi_list", "label_col": 2,
                "row_start": 10, "row_end": 11, "total_row": None,
                "value_cols": [{"col": 3, "parsed_date": "2026-01"}],
                "slots": [{"row": 10, "mode": "blank"}, {"row": 11, "mode": "blank"}],
                "rules": None, "confidence": 0.9}]
    facts = [{"sheet_name": "KPIs", "row": 10, "canonical_metric": None,
              "metric_label": "Custom KPI grid"}]
    catalogue = {
        "Src!r5": _series("Src!r5", 5, "Gross Margin (%)", unit=PCT),
        "Src!r6": _series("Src!r6", 6, "Revenue"),
    }
    maps = [MetricMap(metric="Custom KPI grid", series_id=None, status="unavailable",
                      note="free-form block")]
    proposals, notes = route_additions(catalogue, maps, regions, facts)
    assert any("activated" in n for n in notes)
    # kpi_list ranking puts the percent series first
    assert [p["source_series_id"] for p in proposals] == ["Src!r5", "Src!r6"]


def test_mapped_metrics_do_not_activate_and_used_series_never_proposed():
    regions = [{"sheet_name": "KPIs", "kind": "kpi_list", "label_col": 2,
                "row_start": 10, "row_end": 11, "total_row": None,
                "value_cols": [{"col": 3, "parsed_date": "2026-01"}],
                "slots": [{"row": 10, "mode": "blank"}], "rules": None, "confidence": 0.9}]
    facts = [{"sheet_name": "KPIs", "row": 10, "canonical_metric": "arr",
              "metric_label": "ARR"}]
    catalogue = {"Src!r5": _series("Src!r5", 5, "ARR")}
    maps = [MetricMap(metric="arr", series_id="Src!r5", status="direct")]
    proposals, notes = route_additions(catalogue, maps, regions, facts)
    assert proposals == [] and not any("activated" in n for n in notes)


def test_rank_candidates_prefers_adjustment_lexicon_for_adjustment_rows():
    region = {"kind": "adjustment_rows"}
    a = _series("Src!r5", 5, "Total Revenue")
    b = _series("Src!r6", 6, "One-Time Restructuring")
    c = _series("Src!r7", 7, "Non-Cash Stock Comp")
    ranked = rank_candidates(region, [a, b, c])
    assert [s.id for s in ranked][:2] == ["Src!r6", "Src!r7"]   # lexicon hits first
    assert ranked[-1].id == "Src!r5"                             # ordered, never dropped


def test_addition_review_items_shapes_and_stable_keys():
    applied = [{"sheet_name": "KPIs", "row": 10, "label": "Churn", "cells_written": 3}]
    pending = [{"sheet_name": "P&L", "row": 22, "label": "Integration Costs",
                "slot_mode": "placeholder", "expected_label": "Adjustment 3"}]
    items = addition_review_items(applied, pending, "src.xlsx")
    assert len(items) == 2
    info = next(i for i in items if i["kind"] == "judgment")
    tap = next(i for i in items if i["kind"] == "addition")
    assert "keep it?" in info["question"] and info["suggested_answer"] == "keep"
    assert tap["suggested_answer"] == "approve"
    assert tap["check_spec"]["proposal"]["expected_label"] == "Adjustment 3"
    # keys are content-addressed and value-free -> stable across re-runs
    again = addition_review_items(applied, pending, "other-source.xlsx")
    assert [i["item_key"] for i in again] == [i["item_key"] for i in items]
