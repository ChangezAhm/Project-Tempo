"""Read an Excel number format into a Unit.

A cell's number format is deterministic truth that labels are not: '0.0%' is a
percent, '0.0"x"' is a multiple, '#,##0;[$€-x]' is EUR money. PE templates and
PortCo exports are wildly inconsistent in how they *label* units but the format
code is right there in the cell. We use it for KIND and CURRENCY.

We deliberately do NOT infer SCALE from the format. Excel always stores the true
value; trailing commas ('#,##0,,') only change how it's *displayed*, not what's
stored. So a cell showing "12" can hold 12 or 12,000,000 and the format can't tell
you which — only magnitude can (see units.resolve_scale).
"""

from __future__ import annotations

import re

from app.population.units import Unit

# check distinctive symbols/words before the bare '$' — Excel wraps locale currency
# as '[$€-407]' / '[$£-809]' where a literal '$' marker would otherwise read as USD.
_CCY = [("€", "EUR"), ("£", "GBP"),
        ("eur", "EUR"), ("gbp", "GBP"), ("usd", "USD"), ("us$", "USD"),
        ("$", "USD")]


def parse_number_format(fmt: str | None) -> Unit:
    """Excel format code -> Unit (kind/currency; base=1 for money since the stored
    value is always raw). Unknown when the format carries no numeric/unit signal."""
    if not fmt or not isinstance(fmt, str):
        return Unit(None, None, "unknown")
    f = fmt
    low = f.lower()

    if "%" in f:
        return Unit(None, None, "percent")
    # a multiple: 0.00"x" / 0.0x / #,##0.0\x
    if re.search(r'(?:"x"|\\x|(?<=0)x)\s*;?', low) or low.rstrip(';').endswith("x"):
        return Unit(None, None, "ratio")

    currency = None
    for tok, code in _CCY:
        if tok in low:
            currency = code
            break

    # a numeric format (has # or 0 placeholders) is a money/number cell holding raw
    if currency is not None or re.search(r"[#0]", f):
        return Unit(1.0, currency, "money")
    return Unit(None, None, "unknown")
