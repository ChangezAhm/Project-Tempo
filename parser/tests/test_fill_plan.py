"""Offline proof of the Fill-Plan path: plan -> verify -> execute -> contract.
No API calls — the planner's output is stubbed as SeriesFill entries, mirroring
test_matcher.py's approach for the legacy binder.
"""

from datetime import date

from app.population.apply import apply_links
from app.population.catalogue import build_catalogue
from app.population.contract import apply_decisions, load_decisions, parse_answer
from app.population.execute import execute_plan
from app.population.periods import align_slot
from app.population.schema import MetricMap, PlanIssue
from app.population.verify import blocked_metrics, verify_plan


# --- fixtures: the Meridian shape (monthly source, quarterly KPI template) ---
def _kpi_source():
    cells = [
        {"row": 5, "col": 1, "value": "ARR", "address": "A5"},
        {"row": 5, "col": 3, "value": 143_900, "address": "C5"},
        {"row": 5, "col": 4, "value": 146_700, "address": "D5"},
        {"row": 5, "col": 5, "value": 149_500, "address": "E5"},
        {"row": 6, "col": 1, "value": "Logo churn %", "address": "A6"},
        {"row": 6, "col": 3, "value": 0.017, "address": "C6"},
        {"row": 6, "col": 4, "value": 0.016, "address": "D6"},
        {"row": 6, "col": 5, "value": 0.016, "address": "E6"},
    ]
    return {"sheets": [{"name": "SaaS", "cells": cells}]}


def _kpi_periods():
    return {"SaaS": [
        {"col": 3, "parsed_date": "2026-01", "period_type": "month"},
        {"col": 4, "parsed_date": "2026-02", "period_type": "month"},
        {"col": 5, "parsed_date": "2026-03", "period_type": "month"},
    ]}


def _fact(metric, cell, col, row, pidx, unit, sheet="KPI"):
    return {"sheet_name": sheet, "cell": cell, "col": col, "row": row,
            "canonical_metric": metric, "metric_label": metric, "unit": unit,
            "currency": None, "period_index": pidx, "scenario": "actual",
            "parsed_date": None, "basis": "unknown"}


def _ctx():
    # quarterly template timeline (Q1-26, Q2-26), no existing row magnitudes
    return ({}, {}, {("KPI", 4): date(2026, 1, 1), ("KPI", 5): date(2026, 4, 1)})


def _demand():
    return {"period_count": 4, "period_grain": "monthly", "as_of_date": None,
            "period_count_by_sheet": {"KPI": 4},
            "metrics": [{"metric": "arr", "label": "ARR ($m)"},
                        {"metric": "churn", "label": "Monthly Churn %"}]}


def _plan():
    return [
        MetricMap(metric="arr", series_id="SaaS!r5", confidence=0.95, rollup="end",
                  source_unit="USD'000", target_unit="USD m", sign_flip=False),
        MetricMap(metric="churn", series_id="SaaS!r6", confidence=0.9, rollup="avg",
                  source_unit="%", target_unit="%"),
    ]


def test_plan_executes_quarterly_kpis_end_to_end():
    # The planner declares EVERYTHING (rollup, units); the executor computes the
    # scale from declared units (no magnitude available — empty template) and
    # expands the quarter mechanically. No heuristic in the loop.
    cat = build_catalogue(_kpi_source(), _kpi_periods())
    facts = [_fact("arr", "D5", 4, 5, 0, "USD m"), _fact("churn", "D6", 4, 6, 0, "%")]
    issues = verify_plan(_plan(), cat, facts, _demand(), _ctx(), agg_membership={})
    assert issues == []
    links, unmatched, exec_issues = execute_plan(facts, cat, _plan(), _demand(),
                                                 template_context=_ctx())
    assert not unmatched and not exec_issues
    by = {lk.template_cell: lk for lk in links}
    assert by["D5"].source_cell == "E5" and by["D5"].unit_scale == 1e-3   # '000 -> m, quarter-end
    assert by["D6"].agg_op == "avg" and by["D6"].agg_source_cells == ["SaaS!D6", "SaaS!E6"]
    result = apply_links(facts, _kpi_source(), links, skipped=[])
    vals = {fc.template_cell: fc.value for fc in result.filled}
    assert vals["D5"] == 149.5
    assert abs(vals["D6"] - (0.017 + 0.016 + 0.016) / 3) < 1e-12


def test_verify_flags_missing_rollup_as_grain_unbridgeable():
    cat = build_catalogue(_kpi_source(), _kpi_periods())
    facts = [_fact("arr", "D5", 4, 5, 0, "USD m")]
    plan = [MetricMap(metric="arr", series_id="SaaS!r5", confidence=0.95)]   # no rollup
    issues = verify_plan(plan, cat, facts, _demand(), _ctx(), agg_membership={})
    codes = {i.code for i in issues}
    assert "GRAIN_UNBRIDGEABLE" in codes
    assert "arr" in blocked_metrics(issues)
    # blocked metrics stay blank WITH the reason, never a silent hole
    links, unmatched, _ = execute_plan(facts, cat, plan, _demand(),
                                       template_context=_ctx(),
                                       blocked=blocked_metrics(issues))
    assert not links and "held for review" in unmatched[0]["reason"]


def test_verify_catches_hallucinated_series_and_low_confidence():
    cat = build_catalogue(_kpi_source(), _kpi_periods())
    facts = [_fact("arr", "D5", 4, 5, 0, "USD m")]
    plan = [MetricMap(metric="arr", series_id="SaaS!r99", confidence=0.95, rollup="end"),
            MetricMap(metric="churn", series_id="SaaS!r6", confidence=0.3, rollup="avg")]
    issues = verify_plan(plan, cat, facts, _demand(), _ctx(), agg_membership={})
    codes = {(i.metric, i.code) for i in issues}
    assert ("arr", "SERIES_NOT_FOUND") in codes
    assert ("churn", "LOW_CONFIDENCE") in codes
    lc = next(i for i in issues if i.code == "LOW_CONFIDENCE")
    assert lc.severity == "question" and lc.suggested_resolution   # a question, not a blank


def test_execute_scale_conflict_resolves_to_template_magnitudes():
    # Plan says raw dollars, but the template row already holds ~150 (millions
    # scale): the template's own numbers are stronger evidence — used + flagged.
    cat = build_catalogue(_kpi_source(), _kpi_periods())
    facts = [_fact("arr", "D5", 4, 5, 0, "USD m")]
    ctx = ({}, {("KPI", 5): [150.0]}, _ctx()[2])
    plan = [MetricMap(metric="arr", series_id="SaaS!r5", confidence=0.95, rollup="end",
                      source_unit="USD", target_unit="USD")]   # implies x1 — wrong
    links, unmatched, issues = execute_plan(facts, cat, plan, _demand(), template_context=ctx)
    assert links and links[0].unit_scale == 1e-3                  # evidence won
    assert any(i.code == "SCALE_CONFLICT" and i.resolution for i in issues)
    assert "auto-resolved" in (links[0].note or "")               # visible, never silent


def test_bucket_incomplete_becomes_a_question():
    # Q2-26 slot but the source only has April+May — a partial quarter is never
    # summed; the refusal carries a typed issue for the question tier.
    src = {"sheets": [{"name": "SaaS", "cells": [
        {"row": 5, "col": 1, "value": "Revenue", "address": "A5"},
        {"row": 5, "col": 3, "value": 100.0, "address": "C5"},
        {"row": 5, "col": 4, "value": 110.0, "address": "D5"},
    ]}]}
    periods = {"SaaS": [{"col": 3, "parsed_date": "2026-04", "period_type": "month"},
                        {"col": 4, "parsed_date": "2026-05", "period_type": "month"}]}
    cat = build_catalogue(src, periods)
    facts = [_fact("revenue", "E5", 5, 5, 1, None)]
    ctx = ({}, {}, {("KPI", 4): date(2026, 1, 1), ("KPI", 5): date(2026, 4, 1)})
    plan = [MetricMap(metric="revenue", series_id="SaaS!r5", confidence=0.9, rollup="sum")]
    links, unmatched, issues = execute_plan(facts, cat, plan, _demand(), template_context=ctx)
    assert not links
    assert any(i.code == "BUCKET_INCOMPLETE" and i.severity == "question" for i in issues)


def test_align_slot_reason_codes():
    monthly = [(c, date(2026, c - 1, 1), "month") for c in range(2, 8)]
    assert align_slot(0, 4, date(2026, 1, 1), monthly, "monthly",
                      template_grain="quarter")[1] == "grain_unbridgeable"
    assert align_slot(2, 4, date(2026, 7, 1), monthly, "monthly",
                      template_grain="quarter", rollup="sum")[1] == "no_column_in_bucket"
    assert align_slot(0, 4, date(2026, 1, 1), [], "monthly")[1] == "no_source_periods"


def test_contract_answer_parsing_and_overlay():
    assert parse_answer("rollup", "use the quarter-END value") == "end"
    assert parse_answer("rollup", "average them") == "avg"
    assert parse_answer("confirmed", "Yes — keep this mapping") is True
    assert parse_answer("source_unit", "EUR '000") == "EUR '000"
    fills = [MetricMap(metric="arr", series_id="SaaS!r5", confidence=0.4)]
    n = apply_decisions(fills, {"arr": {"rollup": "end", "confirmed": True}})
    assert n == 2 and fills[0].rollup == "end" and fills[0].confidence >= 0.9
    assert "contract" in (fills[0].note or "")


def test_load_decisions_parses_answered_items(monkeypatch):
    from app.population import contract as C

    rows = [{"status": "answered",
             "check_spec": {"decision": {"metric": "arr", "field": "rollup", "proposal": None}},
             "resolution": {"answer": "quarter-end"}},
            {"status": "open",
             "check_spec": {"decision": {"metric": "x", "field": "rollup"}},
             "resolution": None}]

    class _SB:
        @staticmethod
        def list_review_items(vid):
            return rows

    monkeypatch.setattr("app.supabase_client.list_review_items", _SB.list_review_items)
    dec = load_decisions("v1")
    assert dec == {"arr": {"rollup": "end"}}


def test_plan_issue_model_roundtrip():
    i = PlanIssue(metric="arr", code="SCALE_CONFLICT", detail="x", severity="default",
                  resolution="used magnitude", cells=["KPI!D5"])
    d = i.model_dump()
    assert d["code"] == "SCALE_CONFLICT" and d["cells"] == ["KPI!D5"]


def test_declared_scale_contradicting_template_evidence_asks():
    # An EMPTY row whose plan declares raw->raw, in a template whose ANCHORED
    # rows reconcile at x1e-3: the contradiction is a SCALE question, never a
    # silent x1000-wrong fill (and corroboration passes clean).
    from types import SimpleNamespace
    from app.population.execute import _resolve_scale
    from app.population.units import Unit, resolve_unit
    fill = MetricMap(metric="x", source_unit="USD", target_unit="USD")
    ser = SimpleNamespace(unit=Unit(1.0, "USD", "money"))
    raw = resolve_unit("USD")
    # anchored rows display x1000 (thousands template) but the plan reads it raw
    scale, flag, code = _resolve_scale(fill, ser, [9_800_000], [], raw, tpl_target_base=1e3)
    assert scale is None and code == "SCALE_CONFLICT" and "anchored" in flag
    scale, flag, code = _resolve_scale(fill, ser, [9_800_000], [], raw, tpl_target_base=1.0)
    assert scale == 1.0 and flag is None and code is None   # corroborated


def test_anchor_unfilled_triggers_repair_issue():
    # The saas revenue disaster: a template TOTAL whose feeders are ALL unmapped
    # while source series sit unused must raise ANCHOR_UNFILLED (repair tier) —
    # never a silent empty statement.
    cat = build_catalogue(_kpi_source(), _kpi_periods())     # SaaS!r5 (ARR), r6 unused
    facts = [_fact("subscription", "D5", 4, 5, 0, "EUR m"),
             _fact("services", "D6", 4, 6, 0, "EUR m")]
    plan = [MetricMap(metric="subscription", status="unavailable"),
            MetricMap(metric="services", status="unavailable")]
    demand = {"period_count": 4, "period_grain": "monthly",
              "period_count_by_sheet": {"KPI": 4},
              "metrics": [{"metric": "subscription"}, {"metric": "services"}]}
    membership = {"subscription": frozenset({("KPI", 10)}), "services": frozenset({("KPI", 10)})}
    issues = verify_plan(plan, cat, facts, demand, _ctx(), membership)
    anchor = [i for i in issues if i.code == "ANCHOR_UNFILLED"]
    assert anchor and anchor[0].severity == "repair"
    assert "UNUSED source series" in anchor[0].detail
    # a partially-mapped total is NOT starved
    plan2 = [MetricMap(metric="subscription", series_id="SaaS!r5", confidence=0.9, rollup="end"),
             MetricMap(metric="services", status="unavailable")]
    issues2 = verify_plan(plan2, cat, facts, demand, _ctx(), membership)
    assert not [i for i in issues2 if i.code == "ANCHOR_UNFILLED"]


def test_anchor_rescues_low_confidence_only_hope_as_reconcile():
    # The saas revenue chain: the planner maps the anchor's only feeder at 0.40 —
    # holding it at the floor means an empty statement, so the ladder fills it as
    # a FLAGGED reconcile (visible, questioned) instead.
    cat = build_catalogue(_kpi_source(), _kpi_periods())
    facts = [_fact("subscription", "D5", 4, 5, 0, "EUR m")]
    plan = [MetricMap(metric="subscription", series_id="SaaS!r5", confidence=0.4,
                      rollup="end", source_unit="USD'000", target_unit="USD m")]
    demand = {"period_count": 4, "period_grain": "monthly",
              "period_count_by_sheet": {"KPI": 4}, "metrics": [{"metric": "subscription"}]}
    membership = {"subscription": frozenset({("KPI", 10)})}
    issues = verify_plan(plan, cat, facts, demand, _ctx(), membership)
    assert plan[0].status == "reconcile" and plan[0].assumption      # rescued + flagged
    assert not [i for i in issues if i.code == "LOW_CONFIDENCE"]     # hold lifted
    rescue = [i for i in issues if i.code == "ANCHOR_UNFILLED" and i.severity == "default"]
    assert rescue and rescue[0].resolution
    assert "subscription" not in blocked_metrics(issues)
    links, unmatched, _ = execute_plan(facts, cat, plan, demand,
                                       template_context=_ctx(),
                                       blocked=blocked_metrics(issues))
    assert links and "reconciled" in (links[0].note or "")           # it FILLS, flagged
