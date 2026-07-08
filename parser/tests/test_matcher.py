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
from app.population.schema import MetricMap


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


def test_bind_blocks_non_actual_scenario():
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    links, unmatched = bind([_fact("revenue", "B10", scenario="budget")], cat, maps, _demand())
    assert not links and "budget" in unmatched[0]["reason"]


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


def test_catalogue_from_understanding_excludes_future_budget_columns():
    # A source's budget block carries REAL month dates; if catalogued it would
    # date-match the template's empty future slots (actuals) — must be excluded.
    from app.population.catalogue import catalogue_from_understanding
    und = [{"sheet": "Cash", "periods": [
        {"header_cell": "C3", "date": "2026-06-30", "grain": "month", "kind": "actual"},
        {"header_cell": "D3", "date": "2026-07-31", "grain": "month", "kind": "budget"},
    ], "series": [{"label_cell": "A5", "label": "Cash at bank"}]}]
    cat = catalogue_from_understanding(_ccy_snap(), und, as_of=date(2026, 7, 8))
    cols = [c for (c, _d, _g) in cat["Cash!r5"].period_cols]
    assert cols == [3]   # the Jul-26 budget column is not bindable


def test_catalogue_past_columns_are_actuals_despite_model_tag():
    # The model tags future-looking YEARS 'forecast' — it once mislabeled six
    # months of real P&L actuals and they were dropped. The deterministic date
    # beats the tag: on/before the as-of date == actuals.
    from app.population.catalogue import catalogue_from_understanding
    und = [{"sheet": "Cash", "periods": [
        {"header_cell": "C3", "date": "2026-05-31", "grain": "month", "kind": "forecast"},
        {"header_cell": "D3", "date": "2026-06-30", "grain": "month", "kind": "forecast"},
    ], "series": [{"label_cell": "A5", "label": "Cash at bank"}]}]
    cat = catalogue_from_understanding(_ccy_snap(), und, as_of=date(2026, 7, 8))
    cols = [c for (c, _d, _g) in cat["Cash!r5"].period_cols]
    assert cols == [3, 4]   # both kept — they're dated in the past


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
