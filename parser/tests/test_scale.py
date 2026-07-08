"""Offline proof of the scale fix: scale is decided by MAGNITUDE reconciliation
(what the template cell actually holds), not by unit labels — which are unreliable
across PE/PortCo files. Anything that can't be magnitude-verified is flagged, not
silently written. No API calls.
"""

from app.population.binding import bind
from app.population.catalogue import build_catalogue
from app.population.numfmt import parse_number_format
from app.population.schema import MetricMap
from app.population.units import Unit, resolve_scale, resolve_unit


# --- number format -> kind/currency (deterministic truth) ------------------
def test_parse_number_format_kind_and_currency():
    assert parse_number_format("0.0%").kind == "percent"
    assert parse_number_format('#,##0.0"x"').kind == "ratio"
    assert parse_number_format("0.0x").kind == "ratio"
    eur = parse_number_format('#,##0;[$€-x]')
    assert eur.kind == "money" and eur.currency == "EUR"
    assert parse_number_format("$#,##0").currency == "USD"
    assert parse_number_format("#,##0").kind == "money"      # numeric, no currency
    assert parse_number_format("@").kind == "unknown"        # text
    assert parse_number_format(None).kind == "unknown"


# --- resolve_scale: magnitude wins over labels ----------------------------
_RAW = Unit(1.0, "EUR", "money")   # source stores raw ones


def test_magnitude_reconciliation_picks_scale():
    # source ~12,000,000 ; template row holds ~12 -> scale 1e-6, verified (no flag)
    scale, flag = resolve_scale([12_000_000, 11_000_000], [11.8, 12.1], _RAW, _RAW)
    assert scale == 1e-6 and flag is None


def test_magnitude_overrides_a_wrong_label():
    # both labels say raw ones -> series_scale would give 1.0 and write 12,000,000
    # into a cell whose row holds ~12. Magnitude overrides the label to 1e-6.
    assert resolve_scale([12_000_000], [12.0], _RAW, _RAW) == (1e-6, None)


def test_magnitude_mismatch_is_flagged_not_guessed():
    # 12,000,000 vs 500 doesn't line up to any clean 10^3 step -> flagged
    scale, flag = resolve_scale([12_000_000], [500.0], _RAW, _RAW)
    assert flag is not None and "unverified" in flag


def test_label_scale_without_template_magnitude_is_flagged():
    # fresh template row (no magnitudes); scaling on labels alone -> written but flagged
    millions = resolve_unit("EUR millions")
    scale, flag = resolve_scale([12_000_000], [], _RAW, millions)
    assert scale == 1e-6 and flag == "scale_unverified:no_template_magnitude"


def test_same_unit_no_magnitude_is_not_flagged():
    # raw->raw needs no scaling, so no review noise even without template magnitudes
    scale, flag = resolve_scale([12_000_000], [], _RAW, _RAW)
    assert scale == 1.0 and flag is None


def test_percent_never_scaled_into_money():
    pct = Unit(1.0, None, "percent")
    assert resolve_scale([0.45], [0.46], pct, pct) == (1.0, None)
    scale, flag = resolve_scale([0.45], [12_000_000], pct, _RAW)
    assert scale is None and flag == "unit_kind_mismatch"


# --- catalogue uses the number format for kind ----------------------------
def test_catalogue_reads_percent_from_number_format():
    snap = {"sheets": [{"name": "KPI", "cells": [
        {"row": 4, "col": 1, "value": "Gross margin", "address": "A4"},
        {"row": 4, "col": 3, "value": 0.45, "address": "C4", "style": {"number_format": "0.0%"}},
    ]}]}
    periods = {"KPI": [{"col": 3, "parsed_date": "2023-12", "period_type": "month"}]}
    cat = build_catalogue(snap, periods)
    assert cat["KPI!r4"].unit.kind == "percent"   # despite the label saying nothing about %


# --- end to end through bind with template magnitudes ---------------------
def _source():
    return {"sheets": [{"name": "P&L", "cells": [
        {"row": 5, "col": 1, "value": "Revenue", "address": "A5"},
        {"row": 5, "col": 3, "value": 12_000_000, "address": "C5", "style": {"number_format": "#,##0"}},
    ]}]}


def _periods():
    return {"P&L": [{"col": 3, "parsed_date": "2023-12", "period_type": "month"}]}


def _fact():
    return {"sheet_name": "Model", "cell": "B10", "row": 10, "canonical_metric": "revenue",
            "metric_label": "Revenue", "unit": None, "currency": "EUR",
            "period_index": 0, "scenario": "actual", "parsed_date": None}


def _demand():
    return {"period_count": 1, "period_grain": "monthly",
            "metrics": [{"metric": "revenue", "label": "Revenue", "unit": None}]}


def test_bind_uses_template_magnitudes_to_scale_with_no_labels():
    cat = build_catalogue(_source(), _periods())
    maps = [MetricMap(metric="revenue", series_id="P&L!r5", confidence=0.9)]
    # template row 10 already holds ~12 (a prior actual) -> scale 1e-6 with NO unit labels
    ctx = ({("Model", "B10"): "#,##0.0"}, {("Model", 10): [11.9, 12.0]})
    links, unmatched = bind([_fact()], cat, maps, _demand(), template_context=ctx)
    assert not unmatched and links[0].unit_scale == 1e-6
    # magnitude-confirmed -> no SCALE review flag. (An fx_unverified flag is fine
    # here: the source declares no currency, which is a separate review signal.)
    assert not (links[0].note and "scale_unverified" in links[0].note)
