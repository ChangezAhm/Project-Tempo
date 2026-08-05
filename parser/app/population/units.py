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
from functools import lru_cache

# THE currency lexicon — ordered, distinctive tokens first. Single source of
# truth (catalogue and derive import it; numfmt keeps its format-symbol map).
CCY_TOKENS = [
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


# Count-type series (headcount, FTEs): dimensionless — never magnitude-rescaled.
# The money words ('Employee costs', 'Revenue per FTE')
# must NOT match: a count word next to cost/expense/per means money.

def resolve_unit(text: str | None) -> Unit:
    """Parse a unit label ('$m', "EUR'000", 'EUR millions', '%', 'x', None) into a
    Unit. Money with no explicit scale word is treated as plain ones (base=1),
    which is the common 'raw values' case. Hot path (called per fact/cell), so
    the parse is cached; input is coerced to str for hashability."""
    if text is None:
        return Unit(None, None, "unknown")
    return _resolve_unit(str(text))


@lru_cache(maxsize=512)
def _resolve_unit(raw: str) -> Unit:
    raw = raw.strip()
    if not raw:
        return Unit(None, None, "unknown")
    s = raw.lower()

    if "%" in s or "percent" in s:
        return Unit(1.0, None, "percent")
    # a bare multiple/ratio ('x', '2.5x', 'ratio')
    if s in ("x", "multiple", "ratio") or re.fullmatch(r"\d*\.?\d*x", s):
        return Unit(1.0, None, "ratio")

    currency = None
    for tok, code in CCY_TOKENS:
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


