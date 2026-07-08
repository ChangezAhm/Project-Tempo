"""Deterministic unit & currency resolution.

The #1 cause of garbage numbers was the LLM guessing a unit_scale per cell, so a
single row jumped 1000x mid-series. Here scale is computed ONCE per series from
declared units, deterministically.

A Unit's ``base`` is "how many plain ones does one displayed unit equal":
millions -> 1e6, thousands -> 1e3, plain -> 1. To convert a SOURCE value into the
TEMPLATE's display unit you multiply by ``source.base / template.base``:

    source raw ones  -> template EUR millions :  1 / 1e6   = 1e-6
    source thousands -> template millions     : 1e3 / 1e6  = 1e-3
    source millions  -> template millions     : 1e6 / 1e6  = 1.0

(That 1e-6 is exactly the scale the correct rows in the bad run used — the wrong
rows used 0.001 or 1.0. One scale per series makes that impossible.)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_CCY_TOKENS = [
    ("usd", "USD"), ("us$", "USD"), ("$", "USD"),
    ("eur", "EUR"), ("€", "EUR"),
    ("gbp", "GBP"), ("£", "GBP"),
]

# scale patterns, checked biggest-first; values are "ones per displayed unit"
_SCALE_PATTERNS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"(?:\bbillions?\b|\bbn\b|\bbln\b)"), 1_000_000_000.0),
    (re.compile(r"(?:\bmillions?\b|\bmn\b|\bmm\b|(?<![a-z])m(?![a-z]))"), 1_000_000.0),
    (re.compile(r"(?:\bthousands?\b|(?<![a-z])k(?![a-z])|'?0{3}s?\b)"), 1_000.0),
]


@dataclass(frozen=True)
class Unit:
    base: float | None      # ones per displayed unit; None = unknown scale
    currency: str | None    # USD / EUR / GBP / None
    kind: str               # money | percent | ratio | unknown


# Count-type series (headcount, FTEs): dimensionless — never magnitude-rescaled,
# never FX-converted. The money words ('Employee costs', 'Revenue per FTE')
# must NOT match: a count word next to cost/expense/per means money.
_COUNT = re.compile(r"(?i)\b(headcount|head\s*count|fte|ftes|employees|staff)\b")
_NOT_COUNT = re.compile(r"(?i)cost|expense|salar|compensation|\bper\b")


def is_count_like(text: str | None) -> bool:
    """True when a label names a people/unit COUNT (not a money amount)."""
    return bool(text) and bool(_COUNT.search(text)) and not _NOT_COUNT.search(text)


def resolve_unit(text: str | None) -> Unit:
    """Parse a unit label ('$m', "EUR'000", 'EUR millions', '%', 'x', None) into a
    Unit. Money with no explicit scale word is treated as plain ones (base=1),
    which is the common 'raw values' case."""
    if text is None:
        return Unit(None, None, "unknown")
    raw = str(text).strip()
    if not raw:
        return Unit(None, None, "unknown")
    s = raw.lower()

    if "%" in s or "percent" in s:
        return Unit(1.0, None, "percent")
    # a bare multiple/ratio ('x', '2.5x', 'ratio')
    if s in ("x", "multiple", "ratio") or re.fullmatch(r"\d*\.?\d*x", s):
        return Unit(1.0, None, "ratio")

    currency = None
    for tok, code in _CCY_TOKENS:
        if tok in s:
            currency = code
            break

    base = None
    for pat, val in _SCALE_PATTERNS:
        if pat.search(s):
            base = val
            break

    if currency is not None:
        # money: an explicit scale wins; otherwise assume plain ones
        return Unit(base if base is not None else 1.0, currency, "money")
    if base is not None:
        # a scale word with no currency ('Millions') — still scalable
        return Unit(base, None, "money")
    return Unit(None, None, "unknown")


def series_scale(source: Unit, template: Unit) -> tuple[float | None, str | None]:
    """Multiplier to turn a source value into the template's display unit, plus an
    optional flag when it can't be done safely. Returns (scale, flag)."""
    # mixing money with percent/ratio is a category error — never silently scale
    kinds = {source.kind, template.kind}
    if {"percent"} & kinds or {"ratio"} & kinds:
        if source.kind == template.kind:
            return 1.0, None
        return None, "unit_kind_mismatch"
    if source.base is None or template.base is None:
        return None, "scale_unknown"
    return source.base / template.base, None


def _median_abs(xs) -> float | None:
    vals = sorted(abs(float(x)) for x in (xs or [])
                  if isinstance(x, (int, float)) and not isinstance(x, bool) and x)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


# financial scale steps (ones-per-displayed-unit ratios), 3 decades apart
_SCALE_CANDIDATES = [1e-6, 1e-3, 1.0, 1e3, 1e6]


def reconcile_scale(source_samples, template_samples, tol: float = 0.5) -> float | None:
    """The clean ×10^(3n) that lines source magnitudes up with the template's, or
    None if there's no magnitude on a side or it doesn't snap within `tol` decades.
    tol=0.5 (~3x) for per-cell matching; loosen it for a noisy whole-file estimate."""
    s = _median_abs(source_samples)
    t = _median_abs(template_samples)
    if not s or not t:
        return None
    ratio = t / s
    best = min(_SCALE_CANDIDATES, key=lambda c: abs(math.log10(ratio) - math.log10(c)))
    return best if abs(math.log10(ratio) - math.log10(best)) <= tol else None


def resolve_scale(source_samples, template_samples, source: Unit, template: Unit,
                  fallback_scale: float = 1.0) -> tuple[float | None, str | None]:
    """The robust scale: reconcile the source value's MAGNITUDE against what the
    template cell actually holds, instead of trusting unit labels (which are a mess
    in real PE/PortCo files).

    Priority:
      1. kind guard — never scale a % into money.
      2. magnitude reconciliation — if we have real numbers on both sides, pick the
         ×10^(3n) that lines their magnitudes up. This OVERRIDES labels (it catches
         a template that secretly holds pre-scaled values), and it is self-verifying.
      3. label/format math (series_scale) — when there's no template magnitude yet
         (e.g. a fresh template row). Flag it 'scale_unverified' so a human can
         confirm rather than us silently 1000×-ing.
      4. give up -> (None, reason); the binder leaves the cell blank for review.
    """
    if source.kind in ("percent", "ratio") or template.kind in ("percent", "ratio"):
        if source.kind == template.kind:
            return 1.0, None
        return None, "unit_kind_mismatch"

    s = _median_abs(source_samples)
    t = _median_abs(template_samples)
    mag_flag = None
    if s and t:
        ratio = t / s
        best = min(_SCALE_CANDIDATES, key=lambda c: abs(math.log10(ratio) - math.log10(c)))
        if abs(math.log10(ratio) - math.log10(best)) <= 0.5:   # within ~3x of a clean step
            return best, None                                   # verified by magnitude
        mag_flag = "scale_unverified:magnitude_mismatch"        # numbers don't line up cleanly

    sc, f = series_scale(source, template)
    if sc is not None:
        if mag_flag:                          # magnitude existed but wouldn't reconcile
            return sc, mag_flag
        if t is None and sc != 1.0:           # scaling on labels alone, nothing to confirm it
            return sc, "scale_unverified:no_template_magnitude"
        return sc, None                       # same unit, or magnitude-confirmed earlier

    # No magnitude, no usable labels. Use the scale the REST of this template
    # reconciled to (fallback_scale — e.g. raw→millions = 1e-6), not a blind ×1, so
    # an unanchored row doesn't end up 10^6 out of line with its neighbours. Flagged
    # for review. (Never assumed when the source declares its own scale.)
    if source.kind == "money" and template.kind in ("money", "unknown") and (source.base or 1.0) == 1.0:
        return fallback_scale, "scale_unverified:assumed_default"
    return None, mag_flag or f or "scale_unknown"
