"""Spend firewall for the LLM steps.

The point: a populate (or any LLM run) can NEVER silently rack up cost again.
Before each model call we estimate its worst-case price and check it against a
hard per-run ceiling; if the call would breach the cap we abort *before* sending
it. After each call we add the real usage to the running total. The cap is read
from env ``TEMPO_MAX_RUN_USD`` (default $1.50) so it can be tuned per environment
without code changes.

Pure/standalone (stdlib only) so it can be unit-tested with no API and reused by
every LLM call site (matching today, the rest as they're migrated)."""

from __future__ import annotations

import os
from contextvars import ContextVar

# Approximate list prices, USD per 1,000,000 tokens, (input, output). These are
# for the guard's estimate only — deliberately conservative; tune as pricing
# moves. Unknown models fall back to the most expensive (Opus) so the guard never
# under-estimates.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_FALLBACK = PRICES["claude-opus-4-8"]

# Rough token costs for guarding only (not billing-accurate):
_CHARS_PER_TOKEN = 4          # text heuristic
_TOKENS_PER_IMAGE = 1_600     # a ~150-DPI sheet tile, ballpark


def price_of(model: str) -> tuple[float, float]:
    return PRICES.get(model, _FALLBACK)


def estimate_call_usd(model: str, input_chars: int, max_output_tokens: int, n_images: int = 0) -> float:
    """Worst-case price of one call: all of input_chars as input tokens + every
    output token the call is allowed to emit + any images. Intentionally an
    over-estimate so the cap errs on the side of stopping early."""
    pin, pout = price_of(model)
    in_tok = (max(input_chars, 0) / _CHARS_PER_TOKEN) + n_images * _TOKENS_PER_IMAGE
    return (in_tok * pin + max(max_output_tokens, 0) * pout) / 1_000_000


def default_cap_usd() -> float:
    """Cap for a POPULATE run: one text-first source-understanding pass (Sonnet,
    a handful of sheets) + the metric→series mapping. Low by design; repeat runs on
    the same file are free (cached). Tune via TEMPO_MAX_RUN_USD."""
    try:
        return float(os.environ.get("TEMPO_MAX_RUN_USD", "3.00"))
    except ValueError:
        return 3.00


def default_onboarding_cap_usd() -> float:
    """Cap for ONBOARDING (understanding + enrich): Opus + per-sheet vision, so a
    single sheet call alone can project >$2. This is a one-time-per-template cost,
    so the ceiling is higher than a populate. Tune via TEMPO_MAX_ONBOARDING_USD."""
    try:
        return float(os.environ.get("TEMPO_MAX_ONBOARDING_USD", "60.0"))
    except ValueError:
        return 60.0


class SpendCapExceeded(RuntimeError):
    """Raised before a call that would push a run over its cap."""


class SpendGuard:
    """Tracks USD spent within one run against a hard ceiling. Thread-safe enough
    for the matcher's parallel calls: increments are tiny and the cap is a
    backstop, not an accountant — a small race can't blow past it meaningfully."""

    def __init__(self, cap_usd: float | None = None):
        self.cap = default_cap_usd() if cap_usd is None else float(cap_usd)
        self.spent = 0.0

    def check(self, projected_usd: float) -> None:
        if self.spent + projected_usd > self.cap:
            raise SpendCapExceeded(
                f"Run aborted before this call: it would cost ~${projected_usd:.4f}, "
                f"bringing the run to ~${self.spent + projected_usd:.4f} which exceeds the "
                f"cap of ${self.cap:.2f}. Raise TEMPO_MAX_RUN_USD only if you mean to."
            )

    def record_actual(self, model: str, input_tokens: int, output_tokens: int) -> float:
        pin, pout = price_of(model)
        cost = (input_tokens * pin + output_tokens * pout) / 1_000_000
        self.spent += cost
        return cost


# Per-run guard, set at the top of a run; None means "not inside a guarded run".
_GUARD: ContextVar["SpendGuard | None"] = ContextVar("tempo_spend_guard", default=None)


def set_guard(guard: SpendGuard | None) -> SpendGuard | None:
    _GUARD.set(guard)
    return guard


def get_guard() -> "SpendGuard | None":
    return _GUARD.get()
