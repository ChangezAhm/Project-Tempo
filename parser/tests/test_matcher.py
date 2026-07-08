"""Offline proof of the v4 deterministic matcher: catalogue -> mapping -> binding
-> apply. No API calls. This is the path that replaced the image-OCR matcher that
hallucinated values and guessed 1000x scales; here the correctness is proven for
free, end to end (with the single LLM 'meaning' step stubbed as a fixed mapping).
"""

from datetime import date

from app.population import fx
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


# --- fx multiplier --------------------------------------------------------
def test_fx_multiplier():
    assert fx.multiplier("EUR", "EUR") == (1.0, None)
    assert fx.multiplier(None, "EUR") == (1.0, None)
    assert fx.multiplier("USD", "EUR", rate=0.9) == (0.9, None)
    val, flag = fx.multiplier("USD", "EUR")
    assert val is None and flag == "currency_mismatch:USD->EUR"


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


def test_bind_blocks_currency_mismatch_without_rate():
    cat = build_catalogue(_source_snapshot(), _periods_by_sheet())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    facts = [_fact("revenue", "B10", currency="USD")]   # template USD, source EUR
    links, unmatched = bind(facts, cat, maps, _demand())
    assert not links and "currency_mismatch:EUR->USD" in unmatched[0]["reason"]
    # supply a rate -> it converts (folded into the scale)
    links2, _ = bind(facts, cat, maps, _demand(), fx_rate=1.1)
    assert links2 and abs(links2[0].unit_scale - 1e-6 * 1.1) < 1e-18


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


# --- mapping response parsing --------------------------------------------
def test_mapping_parse_handles_fences():
    text = '```json\n{"mappings":[{"metric":"revenue","series_id":"P&L!r5","confidence":0.9}]}\n```'
    out = _parse(text)
    assert len(out) == 1 and out[0].series_id == "P&L!r5"
    assert _parse("no json here") == []
