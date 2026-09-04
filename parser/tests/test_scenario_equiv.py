"""Scenario equivalence: the budget⇐forecast wall opens ONLY on an answered
contract decision, deterministically, with a flagged note — never inferred."""

from datetime import date

from app.population.catalogue import Series
from app.population.contract import parse_answer
from app.population.execute import execute_plan
from app.population.schema import MetricMap
from app.population.units import Unit
from app.population.verify import verify_plan


def _series():
    return {"MA!r10": Series(
        id="MA!r10", sheet="MA", row=10, label="Revenue",
        period_cols=[(3, date(2026, 7, 31), "month"), (4, date(2026, 8, 31), "month")],
        unit=Unit(1.0, "GBP", "money"), sample=[100.0, 110.0],
        col_scenario={3: "actual", 4: "forecast"})}


def _facts():
    return [
        {"sheet_name": "T", "cell": "C7", "row": 7, "col": 3, "metric_label": "Revenue",
         "canonical_metric": None, "period_index": 0, "scenario": "actual"},
        {"sheet_name": "T", "cell": "D7", "row": 7, "col": 4, "metric_label": "Revenue",
         "canonical_metric": None, "period_index": 1, "scenario": "budget"},
    ]


def _ctx():
    return ({}, {}, {("T", 3): date(2026, 7, 31), ("T", 4): date(2026, 8, 31)})


def _demand():
    return {"period_count": 2, "period_count_by_sheet": {"T": 2},
            "period_grain": "month", "metrics": [{"metric": "Revenue"}]}


def _maps():
    return [MetricMap(metric="Revenue", series_id="MA!r10", confidence=0.9,
                      source_unit="GBP", target_unit="GBP")]


def test_budget_slot_stays_blank_without_a_decision():
    links, unmatched, _ = execute_plan(_facts(), _series(), _maps(), _demand(),
                                       template_context=_ctx())
    assert {lk.template_cell for lk in links} == {"C7"}
    assert any("no budget column" in u["reason"] for u in unmatched)
    issues = verify_plan(_maps(), _series(), _facts(), _demand(), _ctx(), None)
    assert any(i.code == "SCENARIO_NO_SOURCE" for i in issues)


def test_confirmed_equivalence_fills_budget_from_forecast_flagged():
    links, unmatched, _ = execute_plan(_facts(), _series(), _maps(), _demand(),
                                       template_context=_ctx(),
                                       scenario_equiv={"budget": "forecast"})
    by_cell = {lk.template_cell: lk for lk in links}
    assert "D7" in by_cell                       # the budget slot filled
    assert "scenario:budget⇐forecast" in (by_cell["D7"].note or "")
    assert not any("no budget column" in u["reason"] for u in unmatched)
    issues = verify_plan(_maps(), _series(), _facts(), _demand(), _ctx(), None,
                         scenario_equiv={"budget": "forecast"})
    assert not any(i.code == "SCENARIO_NO_SOURCE" for i in issues)


def test_equivalence_never_steals_a_real_budget_column():
    # when the source HAS budget-tagged columns, they win — the substitution
    # only serves slots the demanded scenario cannot serve at all.
    cat = _series()
    s = cat["MA!r10"]
    s.period_cols.append((5, date(2026, 8, 31), "month"))
    s.col_scenario[5] = "budget"
    links, _u, _ = execute_plan(_facts(), cat, _maps(), _demand(),
                                template_context=_ctx(),
                                scenario_equiv={"budget": "forecast"})
    by_cell = {lk.template_cell: lk for lk in links}
    assert by_cell["D7"].source_cell == "E10"    # col 5, the REAL budget column
    assert "per contract" not in (by_cell["D7"].note or "")


def test_parse_answer_scenario_equivalence():
    prop = {"budget": "forecast"}
    assert parse_answer("scenario_equivalence",
                        "yes — use forecast for budget columns", prop) == prop
    assert parse_answer("scenario_equivalence", "no — leave them blank", prop) is None


def test_fy_budget_label_keeps_year_identity():
    # 'FY26 Budget' is a YEAR (2026) under the budget scenario — the scenario
    # word must not erase the time identity (it once fed an annual slot a
    # single November).
    from datetime import date as _date
    from app.structure.temporal_analyzer import _parse_period_label
    p = _parse_period_label("FY26 Budget", 53, _date(2026, 8, 15))
    assert p.period_type == "year" and p.parsed_date == "2026"
    assert str(p.status).lower().endswith("budget")
    p2 = _parse_period_label("FY27 Forecast", 54, _date(2026, 8, 15))
    assert p2.period_type == "year" and p2.parsed_date == "2027"
    p3 = _parse_period_label("Budget", 50, _date(2026, 8, 15))
    assert p3.period_type == "budget"        # a bare scenario word is unchanged


def test_annual_slot_prefers_year_column_never_a_single_month():
    # a year-typed template slot must take the source's FY column (or roll up),
    # never a lone month from inside the year.
    cat = {"MA!r10": Series(
        id="MA!r10", sheet="MA", row=10, label="Revenue",
        period_cols=[(3, date(2026, 11, 30), "month"), (4, date(2026, 12, 31), "month"),
                     (9, date(2026, 12, 31), "year")],
        unit=Unit(1.0, "GBP", "money"), sample=[100.0],
        col_scenario={3: "actual", 4: "actual", 9: "actual"})}
    facts = [{"sheet_name": "T", "cell": "BA7", "row": 7, "col": 53,
              "metric_label": "Revenue", "canonical_metric": None,
              "period_index": 0, "scenario": "actual",
              "period_type": "year", "parsed_date": "2026"}]
    ctx = ({}, {}, {})
    demand = {"period_count": 1, "period_count_by_sheet": {"T": 1},
              "period_grain": "month", "metrics": [{"metric": "Revenue"}]}
    maps = [MetricMap(metric="Revenue", series_id="MA!r10", confidence=0.9,
                      source_unit="GBP", target_unit="GBP", rollup="sum")]
    links, unmatched, _ = execute_plan(facts, cat, maps, demand, template_context=ctx)
    assert links and links[0].source_cell == "I10"    # col 9, the YEAR column
    assert not links[0].agg_source_cells              # single, not a month-sum


def test_mixed_actual_forecast_basis_tags_forecast():
    from app.population.reconcile import _scen_of
    assert _scen_of("Actual / Forecast") == "forecast"
    assert _scen_of("Forecast") == "forecast"
    assert _scen_of("Actual") == "actual"
    assert _scen_of("Budget") == "budget"
    assert _scen_of("Budget / Forecast") is None      # genuinely ambiguous
