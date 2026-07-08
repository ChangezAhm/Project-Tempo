"""Currency policy — STRICT.

The bad run silently wrote USD source values into a EUR template ('FX not applied'
in its own notes). Here, if currencies differ and no rate is supplied, the cell is
NOT written — the binder leaves it blank and flags it — because a wrong number in a
PE deliverable is worse than a blank. Supply an explicit rate to convert.

When exactly ONE side's currency is undetectable we can't prove a mismatch, so the
cell IS written at 1.0 but carries an 'fx_unverified' flag into the link note (the
run's review list picks up 'unverified'), instead of silently pretending both sides
match. Both-unknown is the common no-currency-anywhere case and stays clean.

FX is returned as a plain multiplier so the binder can fold it into a series' unit
scale in one step."""

from __future__ import annotations


def multiplier(source_ccy: str | None, template_ccy: str | None,
               rate: float | None = None) -> tuple[float | None, str | None]:
    """FX as a plain multiplier (so it can be folded into a unit scale).
    same / both-unknown -> (1.0, None); one side unknown -> (1.0, 'fx_unverified')
    for review; differ + valid rate -> (rate, None); differ + no/invalid rate ->
    (None, reason) so the caller leaves the cell blank."""
    if not source_ccy and not template_ccy:
        return 1.0, None
    if not source_ccy or not template_ccy:
        return 1.0, f"fx_unverified:{source_ccy or '?'}->{template_ccy or '?'}"
    if source_ccy == template_ccy:
        return 1.0, None
    if rate is not None:
        try:
            r = float(rate)
        except (TypeError, ValueError):
            return None, f"fx_rate_invalid:{source_ccy}->{template_ccy}"
        if r <= 0:
            return None, f"fx_rate_invalid:{source_ccy}->{template_ccy}"
        return r, None
    return None, f"currency_mismatch:{source_ccy}->{template_ccy}"
