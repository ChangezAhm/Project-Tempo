"""Currency policy — STRICT.

The bad run silently wrote USD source values into a EUR template ('FX not applied'
in its own notes). Here, if currencies differ and no rate is supplied, the cell is
NOT written — the binder leaves it blank and flags it — because a wrong number in a
PE deliverable is worse than a blank. Supply an explicit rate to convert.

FX is returned as a plain multiplier so the binder can fold it into a series' unit
scale in one step."""

from __future__ import annotations


def multiplier(source_ccy: str | None, template_ccy: str | None,
               rate: float | None = None) -> tuple[float | None, str | None]:
    """FX as a plain multiplier (so it can be folded into a unit scale). Same/
    unknown currency -> 1.0; differ + rate -> rate; differ + no rate -> (None,
    reason) so the caller leaves the cell blank and flags it."""
    if not source_ccy or not template_ccy or source_ccy == template_ccy:
        return 1.0, None
    if rate:
        try:
            return float(rate), None
        except (TypeError, ValueError):
            return None, f"fx_rate_invalid:{source_ccy}->{template_ccy}"
    return None, f"currency_mismatch:{source_ccy}->{template_ccy}"
