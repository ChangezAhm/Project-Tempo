"""Offline proof of the unit-scale engine + spend firewall.

No API calls. Period alignment, FX, and end-to-end binding are proven in
test_matcher.py; this file covers unit parsing/scale and the cost firewall — the
parts that decide scale-correctness and cost, proven for free so we never validate
them on a paid run again.
"""

import pytest

from app.population.cost import (
    SpendCapExceeded, SpendGuard, estimate_call_usd, price_of,
)
from app.population.units import Unit, resolve_unit


# --- units: parsing -------------------------------------------------------
def test_resolve_unit_money_scales_and_currency():
    assert resolve_unit("$m") == Unit(1_000_000.0, "USD", "money")
    assert resolve_unit("EUR millions") == Unit(1_000_000.0, "EUR", "money")
    assert resolve_unit("EUR'm") == Unit(1_000_000.0, "EUR", "money")
    assert resolve_unit("$'000") == Unit(1_000.0, "USD", "money")
    assert resolve_unit("USD thousands") == Unit(1_000.0, "USD", "money")
    assert resolve_unit("£bn") == Unit(1_000_000_000.0, "GBP", "money")
    # money with no scale word = plain ones (the common 'raw values' source case)
    assert resolve_unit("$") == Unit(1.0, "USD", "money")
    # a scale word with no currency is still scalable
    assert resolve_unit("Millions") == Unit(1_000_000.0, None, "money")


def test_resolve_unit_non_money_and_unknown():
    assert resolve_unit("%").kind == "percent"
    assert resolve_unit("x").kind == "ratio"
    assert resolve_unit("2.5x").kind == "ratio"
    assert resolve_unit(None).kind == "unknown"
    assert resolve_unit("").kind == "unknown"
    assert resolve_unit("reporting currency / display unit").kind == "unknown"


# --- units: the scale that fixes the 1000x cliff --------------------------
def _rs(source_unit, tgt, mags=()):
    """execute._resolve_scale with a declared source unit — the living scale path."""
    from types import SimpleNamespace
    from app.population.execute import _resolve_scale
    from app.population.schema import MetricMap
    fill = MetricMap(metric="x", source_unit=source_unit)
    ser = SimpleNamespace(unit=resolve_unit("$"))
    return _resolve_scale(fill, ser, [], list(mags), tgt)


def test_declared_units_compute_the_scale():
    millions = resolve_unit("EUR millions")
    assert _rs("$", millions)[0] == 1e-06          # raw -> m
    assert _rs("$'000", millions)[0] == 1e-03      # thousands -> m
    assert _rs("$m", millions)[0] == 1.0           # m -> m


def test_scale_refuses_mismatch_and_asks_when_unknown():
    millions = resolve_unit("EUR millions")
    # % into money is a category error — hard block
    scale, flag, code = _rs("%", millions)
    assert scale is None and code == "UNIT_KIND_MISMATCH"
    # % into % passes at x1
    assert _rs("%", resolve_unit("%"))[0] == 1.0
    # no usable declaration + no magnitudes -> a SCALE question, never a guess
    scale, flag, code = _rs("reporting currency / display unit", millions)
    assert scale is None and code == "SCALE_CONFLICT"


# --- cost: the firewall ---------------------------------------------------
def test_cheaper_model_is_cheaper():
    chars, out_tok = 40_000, 4_000
    opus = estimate_call_usd("claude-opus-4-8", chars, out_tok, n_images=3)
    sonnet = estimate_call_usd("claude-sonnet-4-6", chars, out_tok)
    haiku = estimate_call_usd("claude-haiku-4-5-20251001", chars, out_tok)
    assert opus > sonnet > haiku > 0


def test_unknown_model_falls_back_to_opus_pricing():
    assert price_of("some-future-model") == price_of("claude-opus-4-8")


def test_guard_blocks_before_exceeding_cap():
    g = SpendGuard(cap_usd=0.50)
    g.record_actual("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0)  # $3? no: $3/M*1M=...
    # 1M input tokens on sonnet = $3.00 -> already over a $0.50 cap on next check
    with pytest.raises(SpendCapExceeded):
        g.check(0.01)


def test_guard_allows_within_cap_and_tracks_spend():
    g = SpendGuard(cap_usd=5.00)
    g.check(1.00)                      # fine, nothing spent yet
    spent = g.record_actual("claude-haiku-4-5-20251001", 100_000, 10_000)
    assert spent > 0 and g.spent == spent
    g.check(0.10)                      # still under 5.00


# --- the firewall must reach the worker threads (the bug review caught) ----
def test_guard_does_not_cross_into_plain_threadpool():
    # documents WHY the initializer is required: a contextvar set in the main
    # thread is invisible in ThreadPoolExecutor workers.
    from concurrent.futures import ThreadPoolExecutor
    from app.population.cost import get_guard, set_guard
    set_guard(SpendGuard(1.0))
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            seen = list(ex.map(lambda _: get_guard() is not None, range(2)))
        assert not any(seen)
    finally:
        set_guard(None)


def test_guard_is_enforced_inside_workers_with_initializer():
    from concurrent.futures import ThreadPoolExecutor
    from app.population.cost import get_guard, set_guard
    g = SpendGuard(cap_usd=0.10)
    set_guard(g)
    try:
        def work(_):
            guard = get_guard()          # must be the run's guard, inside the worker
            guard.check(999.0)           # would blow the $0.10 cap
            return "ran"
        with ThreadPoolExecutor(max_workers=2, initializer=set_guard, initargs=(get_guard(),)) as ex:
            with pytest.raises(SpendCapExceeded):
                list(ex.map(work, range(2)))
    finally:
        set_guard(None)
