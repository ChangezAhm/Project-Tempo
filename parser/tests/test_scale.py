"""Offline proof of the scale fix: scale is decided by MAGNITUDE reconciliation
(what the template cell actually holds), not by unit labels — which are unreliable
across PE/PortCo files. Anything that can't be magnitude-verified is flagged, not
silently written. No API calls.
"""

from planpath import bind
from app.population.catalogue import build_catalogue
from app.population.numfmt import parse_number_format
from app.population.schema import MetricMap
from app.population.units import Unit, resolve_unit


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


def test_accounting_spacing_percent_is_not_percent():
    # '_%' is an Excel SPACING directive (gap as wide as '%'), not a percent.
    # This exact format is the production flash template's money format — it was
    # read as percent and the kind guard then blocked every money fill.
    acct = r"_(* #,##0.0_)_%;* \(#,##0.0\)_%;_-??_-;_-@_-"
    u = parse_number_format(acct)
    assert u.kind == "money" and u.currency is None
    # an escaped literal '\%' doesn't scale the value either -> money
    assert parse_number_format(r"0.0\%").kind == "money"
    # a genuine percent format is unchanged
    assert parse_number_format("0.0%").kind == "percent"


def test_bare_locale_tag_on_date_format_is_not_money():
    # '[$-409]' is a locale prefix (empty symbol before the dash) on a DATE
    # format — its '$' must not read as USD money.
    d = parse_number_format("[$-409]dd/mm/yyyy")
    assert d.kind == "unknown" and d.currency is None
    assert parse_number_format("mmm-yy").kind == "unknown"   # date codes → never money
    # non-empty symbol before the dash IS currency; plain numeric/percent unchanged
    eur = parse_number_format("[$€-407]#,##0.00")
    assert eur.kind == "money" and eur.currency == "EUR"
    plain = parse_number_format("#,##0.00")
    assert plain.kind == "money" and plain.currency is None
    assert parse_number_format("0.0%").kind == "percent"


# --- execute._resolve_scale: declared units compute, magnitudes cross-check --
_RAW = Unit(1.0, "EUR", "money")   # source stores raw ones


def _rs(tgt, mags=(), source_unit=None, sample=(12_000_000, 11_000_000)):
    from types import SimpleNamespace
    from app.population.execute import _resolve_scale
    fill = MetricMap(metric="x", source_unit=source_unit)
    return _resolve_scale(fill, SimpleNamespace(unit=_RAW), list(sample), list(mags), tgt)


def test_magnitude_evidence_beats_a_wrong_declaration_with_a_flag():
    # both sides declare raw ones, but the template row holds ~12 against a
    # 12,000,000 source: the template's own numbers win — VISIBLY (flag + issue),
    # never silently as the old label-override did.
    scale, flag, code = _rs(_RAW, mags=[11.8, 12.1])
    assert scale == 1e-6 and code == "SCALE_CONFLICT" and "auto-resolved" in flag


def test_magnitude_mismatch_is_flagged_not_guessed():
    # 12,000,000 vs 500 doesn't line up to any clean 10^3 step -> declared scale
    # used, flagged unverified
    scale, flag, code = _rs(_RAW, mags=[500.0])
    assert scale == 1.0 and flag and "unverified" in flag


def test_declared_scale_without_template_magnitude_is_flagged():
    # fresh template row (no magnitudes); scaling on declarations alone -> written
    # but flagged for review
    millions = resolve_unit("EUR millions")
    scale, flag, code = _rs(millions)
    assert scale == 1e-6 and flag == "scale_unverified:no_template_magnitude"


def test_same_unit_no_magnitude_is_not_flagged():
    # raw->raw needs no scaling, so no review noise even without template magnitudes
    scale, flag, code = _rs(_RAW)
    assert scale == 1.0 and flag is None and code is None


def test_percent_never_scaled_into_money():
    pct = Unit(1.0, None, "percent")
    assert _rs(pct, source_unit="%", sample=[0.45])[0] == 1.0
    scale, flag, code = _rs(_RAW, source_unit="%", sample=[0.45])
    assert scale is None and code == "UNIT_KIND_MISMATCH"


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
    # magnitude-confirmed -> no SCALE review flag
    assert not (links[0].note and "scale_unverified" in links[0].note)
