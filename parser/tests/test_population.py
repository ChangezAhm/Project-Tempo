from app.population.apply import apply_mapping
from app.population.schema import MetricMatch, PeriodAlign, PopulationMapping


def _source():
    # A source workbook: row 5 = Revenue (C/D/E = three months, in thousands),
    # row 6 = Cost of Sales.
    cells = [
        {"address": "B5", "value": "Revenue"},
        {"address": "C5", "value": 1000}, {"address": "D5", "value": 1100}, {"address": "E5", "value": 1200},
        {"address": "B6", "value": "Cost of sales"},
        {"address": "C6", "value": 400}, {"address": "D6", "value": 440},
    ]
    return {"sheets": [{"name": "MgmtAccts", "cells": cells}]}


def _facts():
    # template wants Revenue (rev canonical) for period 0/1/2 and COGS for 0/1
    return [
        {"sheet_name": "PL", "cell": "AD20", "canonical_metric": "revenue", "metric_label": "Net Revenue", "period_index": 0, "scenario": "actual"},
        {"sheet_name": "PL", "cell": "AE20", "canonical_metric": "revenue", "metric_label": "Net Revenue", "period_index": 1, "scenario": "actual"},
        {"sheet_name": "PL", "cell": "AF20", "canonical_metric": "revenue", "metric_label": "Net Revenue", "period_index": 2, "scenario": "actual"},
        {"sheet_name": "PL", "cell": "AD21", "canonical_metric": "cost_of_sales", "metric_label": "Cost of Sales", "period_index": 0, "scenario": "actual"},
    ]


def _mapping():
    return PopulationMapping(
        metric_matches=[
            MetricMatch(template_metric="revenue", source_sheet="MgmtAccts", source_row=5, unit_scale=0.001),
            MetricMatch(template_metric="cost_of_sales", source_sheet="MgmtAccts", source_row=6, unit_scale=0.001),
        ],
        period_aligns=[
            PeriodAlign(source_sheet="MgmtAccts", source_col=3, period_index=0),
            PeriodAlign(source_sheet="MgmtAccts", source_col=4, period_index=1),
            PeriodAlign(source_sheet="MgmtAccts", source_col=5, period_index=2),
        ],
    )


def test_apply_reads_values_deterministically_with_transform():
    r = apply_mapping(_facts(), _source(), _mapping())
    by_cell = {f.template_cell: f for f in r.filled}
    # values read from source, scaled thousands->millions (x0.001)
    assert by_cell["AD20"].value == 1.0 and by_cell["AD20"].source_cell == "C5"
    assert by_cell["AE20"].value == 1.1 and by_cell["AF20"].value == 1.2
    assert by_cell["AD21"].value == 0.4 and by_cell["AD21"].source_cell == "C6"
    assert r.summary == {"facts": 4, "filled": 4, "unmatched": 0}


def test_apply_flags_unmatched():
    facts = _facts() + [
        {"sheet_name": "PL", "cell": "AE21", "canonical_metric": "cost_of_sales", "metric_label": "Cost of Sales", "period_index": 2, "scenario": "actual"},  # no source col mapped? col3 exists; src E6 empty
        {"sheet_name": "PL", "cell": "AD99", "canonical_metric": "ebitda", "metric_label": "EBITDA", "period_index": 0, "scenario": "actual"},  # metric not in source
    ]
    r = apply_mapping(facts, _source(), _mapping())
    reasons = {u["template_cell"]: u["reason"] for u in r.unmatched}
    assert reasons["AD99"] == "no metric match"
    assert reasons["AE21"] == "empty source cell"   # COGS period 2 -> col E -> E6 is empty


def test_scenario_agnostic_match_does_not_bleed_into_budget():
    # source is actuals-only (scenario=None on the match); budget slots must NOT
    # be filled from it, but actual slots should.
    facts = [
        {"sheet_name": "PL", "cell": "AD20", "canonical_metric": "revenue", "metric_label": "Revenue", "period_index": 0, "scenario": "actual"},
        {"sheet_name": "PL", "cell": "BD20", "canonical_metric": "revenue", "metric_label": "Revenue", "period_index": 0, "scenario": "budget"},
    ]
    mapping = PopulationMapping(
        metric_matches=[MetricMatch(template_metric="revenue", scenario=None, source_sheet="MgmtAccts", source_row=5)],
        period_aligns=[PeriodAlign(source_sheet="MgmtAccts", source_col=3, period_index=0)],
    )
    r = apply_mapping(facts, _source(), mapping)
    cells = {f.template_cell for f in r.filled}
    assert "AD20" in cells and "BD20" not in cells
    assert any(u["template_cell"] == "BD20" for u in r.unmatched)


def test_label_match_when_no_canonical():
    facts = [{"sheet_name": "PL", "cell": "Z1", "canonical_metric": None, "metric_label": "Revenue", "period_index": 0, "scenario": "actual"}]
    mapping = PopulationMapping(
        metric_matches=[MetricMatch(template_metric="revenue", source_sheet="MgmtAccts", source_row=5)],
        period_aligns=[PeriodAlign(source_sheet="MgmtAccts", source_col=3, period_index=0)],
    )
    r = apply_mapping(facts, _source(), mapping)
    assert len(r.filled) == 1 and r.filled[0].raw_source_value == 1000
