"""Offline proof of the v4 deterministic matcher: catalogue -> mapping -> binding
-> apply. No API calls. This is the path that replaced the image-OCR matcher that
hallucinated values and guessed 1000x scales; here the correctness is proven for
free, end to end (with the single LLM 'meaning' step stubbed as a fixed mapping).
"""

from datetime import date

from app.population.apply import apply_links
from app.population.binding import _col_letters, bind
from app.population.catalogue import build_catalogue
from app.population.mapping import _parse
from app.population.periods import parse_iso_period, pick_column
from app.population.schema import CellLink, MetricMap


# --- periods --------------------------------------------------------------
def test_parse_iso_period():
    assert parse_iso_period("2023-12") == date(2023, 12, 1)
    assert parse_iso_period("2023-Q3") == date(2023, 7, 1)
    assert parse_iso_period("2023") == date(2023, 1, 1)
    assert parse_iso_period("2023-12-31") == date(2023, 12, 31)
    assert parse_iso_period(None) is None
    assert parse_iso_period("garbage") is None


def test_parse_quarter_first_labels():
    # 'Q1-26' style headers on quarterly dashboards — unparsed they left slots
    # dateless and positional matching pulled a single MONTH into a quarter.
    assert parse_iso_period("Q1-26") == date(2026, 1, 1)
    assert parse_iso_period("Q2 2026") == date(2026, 4, 1)
    assert parse_iso_period("Q3'25") == date(2025, 7, 1)
    assert parse_iso_period("2026 Q4") == date(2026, 10, 1)
    assert parse_iso_period("Q5-26") is None


def _pcols():
    # cols 3,4,5 monthly Oct/Nov/Dec 2023; col 6 is FY2023 (must never fill a month)
    return [
        (3, date(2023, 10, 1), "month"),
        (4, date(2023, 11, 1), "month"),
        (5, date(2023, 12, 1), "month"),
        (6, date(2023, 1, 1), "year"),
    ]


def test_pick_column_by_date_ignores_fy():
    assert pick_column(2, 3, date(2023, 12, 1), _pcols(), "monthly") == 5
    # an FY column can't satisfy a monthly slot even if the year matches
    assert pick_column(0, 3, date(2023, 1, 1), _pcols(), "monthly") is None


def test_pick_column_positional_newest_anchored():
    # relative template (no dates): newest slot -> newest source month
    assert pick_column(2, 3, None, _pcols(), "monthly") == 5   # Dec
    assert pick_column(1, 3, None, _pcols(), "monthly") == 4   # Nov
    assert pick_column(0, 3, None, _pcols(), "monthly") == 3   # Oct
    # template wants more periods than source has -> oldest slot falls off
    assert pick_column(0, 4, None, _pcols(), "monthly") is None


def test_col_letters():
    assert _col_letters(1) == "A"
    assert _col_letters(5) == "E"
    assert _col_letters(27) == "AA"


# --- catalogue ------------------------------------------------------------
def _source_snapshot():
    cells = [
        {"row": 1, "col": 1, "value": "EUR millions", "address": "A1"},
        {"row": 5, "col": 1, "value": "Revenue", "address": "A5"},
        {"row": 5, "col": 3, "value": 10_000_000, "address": "C5"},
        {"row": 5, "col": 4, "value": 11_000_000, "address": "D5"},
        {"row": 5, "col": 5, "value": 12_000_000, "address": "E5"},
        {"row": 5, "col": 6, "value": 33_000_000, "address": "F5"},   # FY total
        {"row": 6, "col": 1, "value": "COGS", "address": "A6"},
        {"row": 6, "col": 5, "value": 4_000_000, "address": "E6"},    # positive in source
    ]
    return {"sheets": [{"name": "P&L", "cells": cells}]}


def _periods_by_sheet():
    return {"P&L": [
        {"col": 3, "parsed_date": "2023-10", "period_type": "month"},
        {"col": 4, "parsed_date": "2023-11", "period_type": "month"},
        {"col": 5, "parsed_date": "2023-12", "period_type": "month"},
        {"col": 6, "parsed_date": "2023", "period_type": "year"},
    ]}


def test_build_catalogue_infers_series_and_unit():
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    assert set(cat) == {"P&L!r5", "P&L!r6"}
    rev = cat["P&L!r5"]
    assert rev.label == "Revenue" and rev.sheet == "P&L"
    # money series, no scale word in label -> raw ones; sheet currency EUR
    assert rev.unit.base == 1.0 and rev.unit.currency == "EUR" and rev.unit.kind == "money"
    assert 12_000_000 in rev.sample


def _demand():
    return {"period_count": 3, "period_grain": "monthly", "as_of_date": None,
            "metrics": [{"metric": "revenue", "label": "Revenue", "unit": "EUR millions"}]}


def _fact(metric, cell, unit="EUR millions", currency="EUR", pidx=2, scenario="actual"):
    return {"sheet_name": "Template", "cell": cell, "canonical_metric": metric,
            "metric_label": metric, "unit": unit, "currency": currency,
            "period_index": pidx, "scenario": scenario, "parsed_date": None}


# --- binding (the correctness core) + apply -------------------------------
def test_bind_scales_raw_to_millions_end_to_end():
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    facts = [_fact("revenue", "B10")]
    links, unmatched = bind(facts, cat, maps, _demand())
    assert not unmatched and len(links) == 1
    assert links[0].source_cell == "E5" and links[0].unit_scale == 1e-6
    # full deterministic read: 12,000,000 raw -> 12.0 millions
    result = apply_links(facts, _source_snapshot(), links, skipped=[])
    assert len(result.filled) == 1 and result.filled[0].value == 12.0


def test_bind_applies_sign_flip():
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="cogs", series_id="P&L!r6", sign_flip=True, confidence=0.9)]
    facts = [_fact("cogs", "B11")]
    links, _ = bind(facts, cat, maps, _demand())
    result = apply_links(facts, _source_snapshot(), links, skipped=[])
    assert result.filled[0].value == -4.0   # 4,000,000 raw, flipped, scaled


# --- sign: template evidence beats the LLM's guess -------------------------
def _rev_cat_positive():
    # Revenue series with clearly positive samples (3 months)
    return build_catalogue(_source_snapshot(), _periods_by_sheet())


def test_sign_row_evidence_overrides_llm_no_flip():
    # Template row already holds NEGATIVES (prior fill: costs negative), source
    # is positive, and the LLM guessed no flip -> the row's evidence wins.
    cat = _rev_cat_positive()
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", sign_flip=False, confidence=0.9)]
    fact = _fact("revenue", "B10")
    fact["row"] = 10
    ctx = ({}, {("Template", 10): [-8.2, -8.5, -8.9]}, {})
    links, _ = bind([fact], cat, maps, _demand(), template_context=ctx)
    assert links and links[0].sign_flip is True
    assert "sign:template-evidence" in (links[0].note or "")


def test_sign_row_evidence_prevents_wrong_llm_flip():
    # Both sides positive but the LLM said flip -> evidence corrects it to False.
    cat = _rev_cat_positive()
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", sign_flip=True, confidence=0.9)]
    fact = _fact("revenue", "B10")
    fact["row"] = 10
    ctx = ({}, {("Template", 10): [8.2, 8.5]}, {})
    links, _ = bind([fact], cat, maps, _demand(), template_context=ctx)
    assert links and links[0].sign_flip is False


def test_sign_l3_convention_used_when_row_is_empty():
    # Fresh template row (no prior values): the L3 sign_convention — derived
    # from the template's own formulas (GP = E8+E9) — decides the flip.
    cat = _rev_cat_positive()
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", sign_flip=False, confidence=0.9)]
    fact = _fact("revenue", "B10")
    fact["sign_convention"] = "negative (entered as negative, added in Gross Profit formula E8+E9)"
    links, _ = bind([fact], cat, maps, _demand())
    assert links and links[0].sign_flip is True


def test_sign_falls_back_to_llm_when_no_evidence():
    # No row values, no convention, single-sample source (no dominant sign on
    # one side is enough to defer): the LLM's sign_flip stands.
    cat = _rev_cat_positive()
    maps = [MetricMap(metric="cogs", series_id="P&L!r6", sign_flip=True, confidence=0.9)]
    links, _ = bind([_fact("cogs", "B11")], cat, maps, _demand())
    assert links and links[0].sign_flip is True


def test_bind_writes_cross_currency_but_flags_it():
    # No FX in the system: differing declared currencies still fill (scale only),
    # but the link carries a review note so the audit can't hide it.
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    facts = [_fact("revenue", "B10", currency="USD")]   # template USD, source EUR
    links, unmatched = bind(facts, cat, maps, _demand())
    assert not unmatched and links[0].unit_scale == 1e-6
    assert "currency_unverified:EUR->USD" in (links[0].note or "")


def test_bind_assumes_no_scaling_for_raw_source_when_unit_unknown():
    # raw source + template with no unit signal and no history: fill at x1 but FLAG
    # for review (better than blank), and never silently scale.
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    facts = [_fact("revenue", "B10", unit="reporting currency / display unit")]
    links, _ = bind(facts, cat, maps, _demand())
    assert links and links[0].unit_scale == 1.0
    assert links[0].note and "assumed_default" in links[0].note
    # an explicit display_unit still forces the real scale
    links2, _ = bind(facts, cat, maps, _demand(), display_unit="EUR millions")
    assert links2 and links2[0].unit_scale == 1e-6


def test_bind_reasons_for_no_map_low_conf_and_no_period():
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    # no mapping at all
    l0, u0 = bind([_fact("revenue", "B10")], cat, [], _demand())
    assert not l0 and "no source series mapped" in u0[0]["reason"]
    # mapped but below the confidence floor
    low = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.4)]
    l1, u1 = bind([_fact("revenue", "B10")], cat, low, _demand())
    assert not l1 and "confidence" in u1[0]["reason"]
    # no source column for an out-of-range period slot
    ok = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    l2, u2 = bind([_fact("revenue", "B10", pidx=9)], cat, ok, _demand())
    assert not l2 and "no source column" in u2[0]["reason"]


def test_bind_budget_demand_without_budget_source_is_unmatched():
    # The deterministic catalogue tags every column 'actual'. A slot that
    # explicitly wants BUDGET has no budget column to bind -> honest unmatched
    # (not a blanket "actuals only" refusal).
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    links, unmatched = bind([_fact("revenue", "B10", scenario="budget")], cat, maps, _demand())
    assert not links and "budget" in unmatched[0]["reason"]


def test_bind_budget_demand_fills_from_budget_column():
    # When the source HAS a budget column for the period, a budget slot binds it —
    # scenario is matched, not dropped.
    from app.population.catalogue import catalogue_from_understanding
    und = [{"sheet": "Cash", "periods": [
        {"header_cell": "C3", "date": "2023-12-31", "grain": "month", "kind": "budget"},
    ], "series": [{"label_cell": "A5", "label": "Cash at bank"}]}]
    cat = catalogue_from_understanding(_ccy_snap(), und)
    maps = [MetricMap(metric="cash", series_id="Cash!r5", confidence=0.9)]
    f = _fact("cash", "B10", unit=None, currency=None, pidx=2, scenario="budget")
    links, unmatched = bind([f], cat, maps, _demand())
    assert links and links[0].source_cell == "C5" and not unmatched


def test_bind_unknown_scenario_prefers_actual_on_tie():
    # Two columns share the same month — one actual, one budget. An unflagged slot
    # (scenario 'unknown') takes the ACTUAL, never the budget.
    snap = {"sheets": [{"name": "Cash", "cells": [
        {"row": 5, "col": 1, "value": "Cash at bank", "address": "A5"},
        {"row": 5, "col": 3, "value": 100, "address": "C5"},   # actual Dec
        {"row": 5, "col": 4, "value": 999, "address": "D5"},   # budget Dec
    ]}]}
    from app.population.catalogue import catalogue_from_understanding
    und = [{"sheet": "Cash", "periods": [
        {"header_cell": "C3", "date": "2023-12-31", "grain": "month", "kind": "actual"},
        {"header_cell": "D3", "date": "2023-12-31", "grain": "month", "kind": "budget"},
    ], "series": [{"label_cell": "A5", "label": "Cash at bank"}]}]
    cat = catalogue_from_understanding(snap, und)
    maps = [MetricMap(metric="cash", series_id="Cash!r5", confidence=0.9)]
    f = _fact("cash", "B10", unit=None, currency=None, pidx=0, scenario="unknown")
    f["col"] = 2
    ctx = ({}, {}, {("Template", 2): date(2023, 12, 31)})
    links, _ = bind([f], cat, maps, _demand(), template_context=ctx)
    assert links and links[0].source_cell == "C5"   # actual, not budget D5


def test_bind_one_sided_unknown_currency_is_clean():
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    # only one side declares a currency -> nothing to compare, no flag
    links, unmatched = bind([_fact("revenue", "B10", currency=None)], cat, maps, _demand())
    assert links and "currency" not in (links[0].note or "")


def test_bind_uses_per_sheet_period_count():
    # workbook-global count (5, from a longer sheet) would positionally misalign
    # this 3-period sheet; the per-sheet count keeps newest-slot -> newest-col.
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    d = _demand()
    d["period_count"] = 5
    d["period_count_by_sheet"] = {"Template": 3}
    links, _ = bind([_fact("revenue", "B10")], cat, maps, d)
    assert links and links[0].source_cell == "E5"   # Dec, not Oct


def test_bind_headcount_is_never_scaled_or_fxed():
    # A count series on a 'USD' sheet gets classified money; with no template
    # magnitude anchor the money fallback once wrote 512 FTEs as 0.000512.
    snap = {"sheets": [{"name": "SaaS", "cells": [
        {"row": 7, "col": 1, "value": "Total employees (FTE)", "address": "A7"},
        {"row": 7, "col": 3, "value": 512, "address": "C7"},
    ]}]}
    periods = {"SaaS": [{"col": 3, "parsed_date": "2023-12", "period_type": "month"}]}
    cat = build_catalogue(snap, periods)
    maps = [MetricMap(metric="headcount", series_id="SaaS!r7", confidence=0.95)]
    fact = _fact("headcount", "D6", unit=None, currency=None)
    fact["metric_label"] = "Headcount (FTE)"
    links, unmatched = bind([fact], cat, maps, _demand())
    assert not unmatched and links[0].unit_scale == 1.0
    assert not links[0].note or "unverified" not in links[0].note   # no scale review noise


def test_is_count_like_words():
    from app.population.units import is_count_like
    assert is_count_like("Headcount (FTE)")
    assert is_count_like("Total employees (FTE)")
    assert not is_count_like("Employee costs")          # money, not a count
    assert not is_count_like("Revenue per FTE (€k)")    # ratio of money to count
    assert not is_count_like("Net Revenue")


def _ccy_snap():
    return {"sheets": [{"name": "Cash", "cells": [
        {"row": 5, "col": 1, "value": "Cash at bank", "address": "A5"},
        {"row": 5, "col": 3, "value": 6_420_000, "address": "C5"},
        {"row": 5, "col": 4, "value": 6_900_000, "address": "D5"},
    ]}]}


def test_catalogue_recovers_undated_source_period_from_cached_header():
    # The AI often returns date=null for a formula/complex source header. The date is
    # recovered from the header cell's COMPUTED (cached) value so the source aligns.
    from app.population.catalogue import catalogue_from_understanding
    snap = {"sheets": [{"name": "P&L", "cells": [
        {"row": 3, "col": 3, "value": "=EOMONTH(AsOf,-1)", "cached_value": "2025-12-31T00:00:00", "address": "C3"},
        {"row": 5, "col": 1, "value": "Revenue", "address": "A5"},
        {"row": 5, "col": 3, "value": 1000, "address": "C5"},
    ]}]}
    und = [{"sheet": "P&L", "periods": [{"header_cell": "C3", "date": None, "grain": "month", "kind": "actual"}],
            "series": [{"label_cell": "A5", "label": "Revenue"}]}]
    cat = catalogue_from_understanding(snap, und)
    assert cat["P&L!r5"].period_cols[0][1] == date(2025, 12, 31)   # recovered, not None


def test_pick_column_positional_when_source_has_no_dates():
    # Source columns all undated (understanding couldn't date them) -> align by
    # position, newest-anchored, even though the template slot HAS a date.
    undated = [(3, None, "month"), (4, None, "month"), (5, None, "month")]
    assert pick_column(2, 3, date(2026, 5, 1), undated, "monthly") == 5   # newest slot -> newest col
    assert pick_column(1, 3, date(2026, 4, 1), undated, "monthly") == 4
    assert pick_column(0, 3, date(2026, 3, 1), undated, "monthly") == 3
    assert pick_column(0, 5, None, undated, "monthly") is None            # out of range


def _row_scenario_snap():
    # metric rows with a bare 'Budget' row under each (flash-pack layout)
    return {"sheets": [{"name": "P&L", "cells": [
        {"row": 3, "col": 3, "value": "2025-01-31T00:00:00", "address": "C3"},
        {"row": 5, "col": 1, "value": "Cost of Goods Sold", "address": "A5"},
        {"row": 5, "col": 3, "value": 400, "address": "C5"},
        {"row": 6, "col": 1, "value": "Budget", "address": "A6"},
        {"row": 6, "col": 3, "value": 450, "address": "C6"},
    ]}]}


def _row_scenario_und():
    return [{"sheet": "P&L",
             "periods": [{"header_cell": "C3", "date": "2025-01-31", "grain": "month", "kind": "actual"}],
             "series": [{"label_cell": "A5", "label": "Cost of Goods Sold"},
                        {"label_cell": "A6", "label": "Budget"}]}]


def test_catalogue_attaches_budget_row_as_variant():
    # A bare 'Budget' row is the budget VARIANT of the metric row above: attached to
    # the parent, renamed, and hidden from the mapper-facing catalogue.
    from app.population.catalogue import catalogue_from_understanding
    cat = catalogue_from_understanding(_row_scenario_snap(), _row_scenario_und())
    assert set(cat) == {"P&L!r5"}                      # the Budget row is not standalone
    parent = cat["P&L!r5"]
    var = parent.variants["budget"]
    assert var.row == 6 and var.label == "Cost of Goods Sold (budget)"


def test_parse_scenario_variant_patterns():
    from app.population.catalogue import parse_scenario_variant as p
    assert p("Budget") == ("budget", None)
    assert p("Forecast:") == ("forecast", None)
    assert p("Plan") == ("forecast", None)
    assert p("Budget (Revenue)") == ("budget", "Revenue")
    assert p("Revenue (Budget)") == ("budget", "Revenue")
    assert p("Revenue - Budget") == ("budget", "Revenue")
    assert p("Budget - Cost of Goods Sold") == ("budget", "Cost of Goods Sold")
    assert p("Revenue") == (None, None)
    assert p("Budget variance %") == (None, None)      # not a scenario tag
    assert p("Budget vs Actual") == (None, None)


def test_catalogue_attaches_named_variant_regardless_of_position():
    # 'Budget (Revenue)' names its parent explicitly — attaches to Revenue even when
    # it is NOT the row directly below it.
    from app.population.catalogue import catalogue_from_understanding
    snap = {"sheets": [{"name": "P&L", "cells": [
        {"row": 5, "col": 3, "value": 100, "address": "C5"},
        {"row": 6, "col": 3, "value": 200, "address": "C6"},
        {"row": 9, "col": 3, "value": 110, "address": "C9"},
    ]}]}
    und = [{"sheet": "P&L",
            "periods": [{"header_cell": "C3", "date": "2025-01-31", "grain": "month", "kind": "actual"}],
            "series": [{"label_cell": "A5", "label": "Revenue"},
                       {"label_cell": "A6", "label": "COGS"},
                       {"label_cell": "A9", "label": "Budget (Revenue)"}]}]
    cat = catalogue_from_understanding(snap, und)
    assert set(cat) == {"P&L!r5", "P&L!r6"}
    assert cat["P&L!r5"].variants["budget"].row == 9   # attached to Revenue, not COGS


def test_catalogue_dataless_budget_row_is_a_heading_not_a_variant():
    # A bare 'Budget' row with NO data is a SECTION HEADING — it must not be
    # swallowed as a variant of the metric above it.
    from app.population.catalogue import catalogue_from_understanding
    snap = {"sheets": [{"name": "P&L", "cells": [
        {"row": 5, "col": 3, "value": 100, "address": "C5"},
    ]}]}
    und = [{"sheet": "P&L",
            "periods": [{"header_cell": "C3", "date": "2025-01-31", "grain": "month", "kind": "actual"}],
            "series": [{"label_cell": "A5", "label": "Revenue"},
                       {"label_cell": "A7", "label": "Budget"}]}]   # heading, no numbers
    cat = catalogue_from_understanding(snap, und)
    assert cat["P&L!r5"].variants == {}                 # not attached
    assert "P&L!r7" in cat                              # left standalone


def test_catalogue_ai_variant_of_attaches_normal_labelled_row():
    # A budget BLOCK repeats the metric names (row 9 'Revenue' again). No label tag —
    # only the AI's per-row judgment (scenario+variant_of) can pair it. It attaches as
    # the variant BUT stays visible to the mapper (an LLM tag never hard-gates data).
    from app.population.catalogue import catalogue_from_understanding
    snap = {"sheets": [{"name": "P&L", "cells": [
        {"row": 5, "col": 3, "value": 100, "address": "C5"},
        {"row": 9, "col": 3, "value": 110, "address": "C9"},
    ]}]}
    und = [{"sheet": "P&L",
            "periods": [{"header_cell": "C3", "date": "2025-01-31", "grain": "month", "kind": "actual"}],
            "series": [{"label_cell": "A5", "label": "Revenue"},
                       {"label_cell": "A9", "label": "Revenue",
                        "scenario": "budget", "variant_of": "Revenue"}]}]
    cat = catalogue_from_understanding(snap, und)
    assert cat["P&L!r5"].variants["budget"].row == 9    # paired by the AI's judgment
    assert "P&L!r9" in cat                              # NOT removed — label didn't confirm
    assert cat["P&L!r9"].scenario == "budget"


def test_catalogue_ai_tag_beats_nothing_and_label_confirms_removal():
    # When the label ALSO confirms ('Budget'), the variant is hidden from the mapper.
    from app.population.catalogue import catalogue_from_understanding
    cat = catalogue_from_understanding(_row_scenario_snap(), [{
        "sheet": "P&L",
        "periods": [{"header_cell": "C3", "date": "2025-01-31", "grain": "month", "kind": "actual"}],
        "series": [{"label_cell": "A5", "label": "Cost of Goods Sold"},
                   {"label_cell": "A6", "label": "Budget",
                    "scenario": "budget", "variant_of": "Cost of Goods Sold"}]}])
    assert set(cat) == {"P&L!r5"}
    assert cat["P&L!r5"].variants["budget"].row == 6


def test_bind_budget_slot_fills_from_variant_row():
    # A template budget slot binds the parent's budget VARIANT row — the
    # scenario-by-row counterpart of a budget column.
    from app.population.catalogue import catalogue_from_understanding
    cat = catalogue_from_understanding(_row_scenario_snap(), _row_scenario_und())
    maps = [MetricMap(metric="cogs", series_id="P&L!r5", confidence=0.9)]
    bud = _fact("cogs", "B10", unit=None, currency=None, pidx=0, scenario="budget")
    act = _fact("cogs", "B11", unit=None, currency=None, pidx=0, scenario="unknown")
    d = {"period_count": 1, "period_grain": "monthly", "as_of_date": None,
         "period_count_by_sheet": {"Template": 1}, "metrics": []}
    links, unmatched = bind([bud, act], cat, maps, d)
    assert not unmatched and len(links) == 2
    by_cell = {lk.template_cell: lk for lk in links}
    assert by_cell["B10"].source_cell == "C6"          # budget slot -> variant row
    assert by_cell["B11"].source_cell == "C5"          # unflagged slot -> metric row
    result = apply_links([bud, act], _row_scenario_snap(), links, skipped=[])
    vals = {f.template_cell: f.value for f in result.filled}
    assert vals["B10"] == 450.0 and vals["B11"] == 400.0


def test_derive_row_scenario_layout_repairs_facts():
    # Pure post-process: bare 'Budget' rows inherit the metric row above; metric
    # rows painted 'budget' by a scenario region flip back to their own label's
    # scenario (unknown here) — the region is ignored on row-scenario sheets.
    from app.datamodel.derive import apply_row_scenario_layout
    from app.datamodel.schema import Basis, DataPoint, Provenance, Scenario

    def dp(row, label, scen, cell):
        return DataPoint(fact_key="x", sheet_name="P&L", cell=cell, row=row, col=3,
                         metric_row_id=None, metric_label=label, canonical_metric=None,
                         period_index=0, period_label="Jan-25", parsed_date="2025-01",
                         period_type="monthly", scenario=scen, basis=Basis.flow,
                         entity=None, unit=None, currency=None, value_role=None,
                         sign_convention=None, qualification_criteria=None, definition=None,
                         expected_source=None, needs_value=True,
                         scenario_source=Provenance.deterministic, basis_source=Provenance.default)

    facts = [dp(5, "Cost of Goods Sold", Scenario.budget, "C5"),   # painted wrong by a region
             dp(6, "Budget", Scenario.budget, "C6")]                # variant row, no identity
    changed = apply_row_scenario_layout(facts, {"P&L": "income_statement"})
    assert changed >= 2
    assert facts[0].scenario == Scenario.unknown                    # region ignored
    assert facts[1].metric_label == "Cost of Goods Sold"            # identity inherited
    assert facts[1].scenario == Scenario.budget
    assert facts[0].fact_key != "x" and facts[1].fact_key != "x"    # keys recomputed


def test_derive_l3_row_tags_pair_budget_block_rows():
    # A budget BLOCK repeats the metric names lower down — no label tag can pair
    # them; the model's per-row judgment (scenario + parent row) does. Identity
    # inheritance is allowed here because the labels already match.
    from app.datamodel.derive import apply_row_scenario_layout
    from app.datamodel.schema import Basis, DataPoint, Provenance, Scenario

    def dp(row, label, cell):
        return DataPoint(fact_key="x", sheet_name="P&L", cell=cell, row=row, col=3,
                         metric_row_id=None, metric_label=label, canonical_metric=None,
                         period_index=0, period_label="Jan-25", parsed_date="2025-01",
                         period_type="monthly", scenario=Scenario.unknown, basis=Basis.flow,
                         entity=None, unit=None, currency=None, value_role=None,
                         sign_convention=None, qualification_criteria=None, definition=None,
                         expected_source=None, needs_value=True,
                         scenario_source=Provenance.default, basis_source=Provenance.default)

    facts = [dp(5, "Revenue", "C5"), dp(20, "Revenue", "C20")]
    # the model judged: row 20 is the budget restatement of row 5
    tags = {"P&L": {20: ("budget", 5)}}
    apply_row_scenario_layout(facts, {"P&L": "income_statement"}, tags)
    assert facts[0].scenario == Scenario.unknown          # primary row untouched
    assert facts[1].scenario == Scenario.budget           # paired by the model
    assert facts[1].metric_label == "Revenue"
    assert facts[1].scenario_source == Provenance.llm     # provenance is honest


def test_catalogue_keeps_all_columns_and_tags_scenario():
    # Every source column is KEPT; each carries its own scenario tag. A budget
    # column is not dropped — binding decides whether it may fill a given slot.
    from app.population.catalogue import catalogue_from_understanding
    und = [{"sheet": "Cash", "periods": [
        {"header_cell": "C3", "date": "2026-06-30", "grain": "month", "kind": "actual"},
        {"header_cell": "D3", "date": "2026-07-31", "grain": "month", "kind": "budget"},
    ], "series": [{"label_cell": "A5", "label": "Cash at bank"}]}]
    cat = catalogue_from_understanding(_ccy_snap(), und)
    s = cat["Cash!r5"]
    assert [c for (c, _d, _g) in s.period_cols] == [3, 4]   # nothing dropped
    assert s.col_scenario == {3: "actual", 4: "budget"}


def test_catalogue_scenario_tag_is_independent_of_as_of():
    # as-of no longer classifies scenario: a forecast-tagged column is kept and
    # stays 'forecast' whether or not an as-of is supplied (a time series carries
    # data before and after the as-of alike).
    from app.population.catalogue import catalogue_from_understanding
    und = [{"sheet": "Cash", "periods": [
        {"header_cell": "C3", "date": "2026-05-31", "grain": "month", "kind": "forecast"},
        {"header_cell": "D3", "date": "2026-06-30", "grain": "month", "kind": "actual"},
    ], "series": [{"label_cell": "A5", "label": "Cash at bank"}]}]
    without = catalogue_from_understanding(_ccy_snap(), und)                       # no as_of
    withas = catalogue_from_understanding(_ccy_snap(), und, as_of=date(2026, 7, 8))
    for cat in (without, withas):
        s = cat["Cash!r5"]
        assert [c for (c, _d, _g) in s.period_cols] == [3, 4]
        assert s.col_scenario == {3: "forecast", 4: "actual"}


def test_bind_and_apply_aggregate_sum_of_series():
    # Template 'Total Revenue' = the sum of three regional turnover lines the
    # source has no single total for. Binding emits ONE derived link citing all
    # three cells; apply sums them; scale reconciles against the TOTAL (33m).
    snap = {"sheets": [{"name": "P&L", "cells": [
        {"row": 5, "col": 1, "value": "Revenue NA", "address": "A5"},
        {"row": 5, "col": 3, "value": 10_000_000, "address": "C5"},
        {"row": 6, "col": 1, "value": "Revenue EMEA", "address": "A6"},
        {"row": 6, "col": 3, "value": 12_000_000, "address": "C6"},
        {"row": 7, "col": 1, "value": "Revenue APAC", "address": "A7"},
        {"row": 7, "col": 3, "value": 11_000_000, "address": "C7"},
    ]}]}
    periods = {"P&L": [{"col": 3, "parsed_date": "2023-12", "period_type": "month"}]}
    cat = build_catalogue(snap, periods)
    maps = [MetricMap(metric="revenue", series_id="P&L!r5",
                      also_series_ids=["P&L!r6", "P&L!r7"], confidence=0.9)]
    fact = _fact("revenue", "B10", pidx=0)
    fact["row"] = 10
    ctx = ({}, {("Template", 10): [33.0]}, {})   # template row is ~33 (millions)
    demand = {"period_count": 1, "period_grain": "monthly", "as_of_date": None,
              "period_count_by_sheet": {"Template": 1}, "metrics": []}
    links, unmatched = bind([fact], cat, maps, demand, template_context=ctx)
    assert not unmatched and len(links) == 1
    lk = links[0]
    assert lk.source_cell == "C5" and lk.agg_source_cells == ["P&L!C6", "P&L!C7"]
    assert "SUM:" in (lk.note or "") and "derived:sum" in (lk.note or "")
    result = apply_links([fact], snap, links, skipped=[])
    assert len(result.filled) == 1 and result.filled[0].value == 33.0   # (10+12+11)m


def test_bind_reconcile_fills_below_floor_and_flags():
    # A reconcile is a deliberate approximation (source cut differently): it fills
    # even below the confidence floor, and the note is flagged for the reviewer.
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", status="reconcile",
                      assumption="source combines lines; assigned here", confidence=0.4)]
    links, unmatched = bind([_fact("revenue", "B10")], cat, maps, _demand())
    assert links and not unmatched
    assert "reconciled:" in (links[0].note or "")
    # a DIRECT map at the same low confidence is still dropped (floor unchanged)
    direct = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.4)]
    l2, u2 = bind([_fact("revenue", "B10")], cat, direct, _demand())
    assert not l2 and "confidence" in u2[0]["reason"]


def test_bind_prevents_double_use_of_a_source_series():
    # Two template metrics mapped to the SAME source series must never both fill —
    # that double-counts the amount. The stronger mapping (direct > reconcile) keeps
    # it; the other is blocked and left blank.
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [
        MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9),            # direct -> wins
        MetricMap(metric="other", series_id="P&L!r5", status="reconcile",
                  assumption="same series", confidence=0.95),                        # blocked despite higher conf
    ]
    facts = [_fact("revenue", "B10"), _fact("other", "B11")]
    links, unmatched = bind(facts, cat, maps, _demand())
    assert len(links) == 1 and links[0].template_cell == "B10"
    assert any("double counting" in u["reason"] for u in unmatched)


def test_bind_double_use_guard_covers_aggregated_components():
    # A component claimed by an aggregate cannot also be used alone by another metric.
    snap = {"sheets": [{"name": "P&L", "cells": [
        {"row": 5, "col": 1, "value": "S&M", "address": "A5"}, {"row": 5, "col": 3, "value": 3_000_000, "address": "C5"},
        {"row": 6, "col": 1, "value": "G&A", "address": "A6"}, {"row": 6, "col": 3, "value": 2_000_000, "address": "C6"},
    ]}]}
    cat = build_catalogue(snap, {"P&L": [{"col": 3, "parsed_date": "2023-12", "period_type": "month"}]})
    maps = [
        MetricMap(metric="Other Opex", series_id="P&L!r5", also_series_ids=["P&L!r6"],
                  status="aggregate", confidence=0.9),                 # claims r5 + r6
        MetricMap(metric="Staff Costs", series_id="P&L!r5", status="reconcile",
                  assumption="x", confidence=0.9),                     # r5 already claimed -> blocked
    ]
    facts = [_fact("Other Opex", "B10", pidx=2), _fact("Staff Costs", "B11", pidx=2)]
    links, unmatched = bind(facts, cat, maps, _demand())
    assert len(links) == 1 and links[0].template_cell == "B10"
    assert any("double counting" in u["reason"] and "Other Opex" in u["reason"] for u in unmatched)


def test_bind_reconcile_can_aggregate_into_residual():
    # The opex case: source S&M + G&A summed onto the template's residual line,
    # flagged reconciled — one general mechanism, not a special case.
    snap = {"sheets": [{"name": "P&L", "cells": [
        {"row": 5, "col": 1, "value": "Sales & marketing", "address": "A5"},
        {"row": 5, "col": 3, "value": 3_000_000, "address": "C5"},
        {"row": 6, "col": 1, "value": "General & admin", "address": "A6"},
        {"row": 6, "col": 3, "value": 2_000_000, "address": "C6"},
    ]}]}
    periods = {"P&L": [{"col": 3, "parsed_date": "2023-12", "period_type": "month"}]}
    cat = build_catalogue(snap, periods)
    maps = [MetricMap(metric="Other Opex", series_id="P&L!r5", also_series_ids=["P&L!r6"],
                      status="reconcile", assumption="source splits opex by function; summed into other opex",
                      confidence=0.5)]
    fact = _fact("Other Opex", "B10", pidx=0); fact["row"] = 10
    ctx = ({}, {("Template", 10): [5.0]}, {})
    d = {"period_count": 1, "period_grain": "monthly", "as_of_date": None,
         "period_count_by_sheet": {"Template": 1}, "metrics": []}
    links, _ = bind([fact], cat, maps, d, template_context=ctx)
    assert links and links[0].agg_source_cells == ["P&L!C6"]
    assert "reconciled:" in (links[0].note or "") and "SUM:" in (links[0].note or "")
    result = apply_links([fact], snap, links, skipped=[])
    assert result.filled[0].value == 5.0   # (3+2)m into other opex


def test_apply_aggregation_never_writes_partial_total():
    # Trust-first: if a component cell is empty at this period, NO partial sum is
    # written — the cell is reported unmatched instead of a wrong number.
    snap = {"sheets": [{"name": "P&L", "cells": [
        {"row": 5, "col": 3, "value": 10_000_000, "address": "C5"},
        # C6 (the EMEA component) is absent for this period
        {"row": 7, "col": 3, "value": 11_000_000, "address": "C7"},
    ]}]}
    fact = _fact("revenue", "B10", pidx=0)
    link = CellLink(template_sheet="Template", template_cell="B10", source_sheet="P&L",
                    source_cell="C5", agg_source_cells=["P&L!C6", "P&L!C7"], unit_scale=1e-6)
    result = apply_links([fact], snap, [link], skipped=[])
    assert not result.filled
    assert any("aggregation incomplete" in u["reason"] for u in result.unmatched)


def test_point_in_time_quarter_slot_accepts_quarter_end_month():
    # A quarterly BALANCE-SHEET slot equals its period-END value: with no
    # quarterly source columns, the month column landing on the bucket's last
    # month (Jun for Q2) satisfies it. Flows never get this.
    # day-of-month is irrelevant to bucket matching — 28th exists in every month
    monthly = [(c, date(2026, m, 28), "month")
               for c, m in [(2, 1), (3, 2), (4, 3), (5, 4), (6, 5), (7, 6)]]
    q2 = date(2026, 6, 30)
    # stock -> Jun column (col 7)
    assert pick_column(1, 8, q2, monthly, "monthly", template_grain="quarter",
                       point_in_time=True) == 7
    # mid-quarter month never satisfies it (Q1 end = Mar, col 4)
    assert pick_column(0, 8, date(2026, 3, 31), monthly, "monthly",
                       template_grain="quarter", point_in_time=True) == 4
    # a FLOW quarterly slot stays blank — one month is not a quarter of P&L
    assert pick_column(1, 8, q2, monthly, "monthly", template_grain="quarter",
                       point_in_time=False) is None


# --- deep rescue: per-metric parallel agents -------------------------------
def test_rescue_metrics_runs_per_metric_and_forces_our_key(monkeypatch):
    from app.population import rescue as R
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    seen = []

    def fake_stream(**kw):
        seen.append(kw["content"])
        # the model echoes a WRONG key ('X') — rescue must overwrite it with ours
        return None, '{"mappings":[{"metric":"X","status":"reconcile","series_id":"P&L!r5",' \
                     '"assumption":"assigned here","confidence":0.7}]}'

    monkeypatch.setattr(R, "guarded_stream", fake_stream)
    metrics = [{"metric": "staff", "label": "Staff Costs"}, {"metric": "opex", "label": "Other Opex"}]
    out = R.rescue_metrics(metrics, cat, used_series={"P&L!r6"})
    assert len(out) == 2 and len(seen) == 2                 # one focused call per metric
    assert {m.metric for m in out} == {"staff", "opex"}     # our keys, not the model's echo
    assert all(m.status == "reconcile" and m.series_id == "P&L!r5" for m in out)


def test_rescue_metrics_is_best_effort_on_failure(monkeypatch):
    from app.population import rescue as R
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())

    def boom(**kw):
        raise RuntimeError("api down")

    monkeypatch.setattr(R, "guarded_stream", boom)
    out = R.rescue_metrics([{"metric": "x", "label": "X"}], cat, used_series=set())
    assert out == []   # a failing agent yields nothing, never raises


# --- mapping: batch retry + loud failure -----------------------------------
def test_map_metrics_retries_bad_json(monkeypatch):
    from app.population import mapping
    calls = {"n": 0}

    def fake_stream(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None, "sorry, here you go:"   # unusable first reply
        return None, '{"mappings":[{"metric":"revenue","series_id":"P&L!r5","confidence":0.9}]}'

    monkeypatch.setattr(mapping, "guarded_stream", fake_stream)
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps, failed = mapping.map_metrics([{"metric": "revenue", "label": "Revenue"}], cat)
    assert failed == 0 and len(maps) == 1 and calls["n"] == 2


def test_map_metrics_counts_dead_batches(monkeypatch):
    from app.population import mapping

    monkeypatch.setattr(mapping, "guarded_stream", lambda **kw: (None, "still garbage"))
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps, failed = mapping.map_metrics([{"metric": "revenue", "label": "Revenue"}], cat)
    assert maps == [] and failed == 1   # dropped loudly, run continues


# --- mapping response parsing --------------------------------------------
def test_mapping_parse_handles_fences():
    text = '```json\n{"mappings":[{"metric":"revenue","series_id":"P&L!r5","confidence":0.9}]}\n```'
    out = _parse(text)
    assert len(out) == 1 and out[0].series_id == "P&L!r5"
    assert _parse("no json here") == []
