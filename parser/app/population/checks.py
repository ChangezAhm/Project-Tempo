"""Post-fill rule checks — captured business logic becomes enforcement (Gap 2).

L3 reads each metric's sign convention from the template's own formulas
(GP = E8+E9 means costs are entered negative). Binding uses that to decide
sign_flip — but evidence can conflict or be missing, so after apply we CHECK
every written value against the fact's declared convention. A violation is
never silently corrected (the value stands — blank-and-explain philosophy
applies to writes we made, too): it is flagged in the run's review list AND
filed as a durable review item, so a human settles it once and the answer
feeds future runs via the context channel.

Deterministic only: prose rules ("all figures in €'000") are surfaced to the
mapper and the reviewer, but never "parsed" into checks — a misparsed rule
enforcing the wrong thing is worse than no check.
"""

from __future__ import annotations

import logging

from app.population.binding import _convention_sign

logger = logging.getLogger(__name__)

# Values this close to zero carry no usable sign signal.
_SIGN_EPSILON = 1e-9


def sign_violations(filled, facts: list[dict]) -> list[dict]:
    """Filled cells whose value contradicts the fact's declared sign
    convention. `filled` = FilledCell models from apply; `facts` = the
    template input facts (carry sign_convention from L3)."""
    conv_by_cell: dict[tuple[str, str], tuple[int, str]] = {}
    for f in facts:
        sc = f.get("sign_convention")
        sign = _convention_sign(sc)
        if sign:
            conv_by_cell[(f.get("sheet_name"), (f.get("cell") or "").upper())] = (sign, str(sc))

    out: list[dict] = []
    for fc in filled:
        key = (fc.template_sheet, (fc.template_cell or "").upper())
        expected = conv_by_cell.get(key)
        if expected is None or not isinstance(fc.value, (int, float)) or isinstance(fc.value, bool):
            continue
        if abs(fc.value) <= _SIGN_EPSILON:
            continue
        exp_sign, rule = expected
        if (fc.value > 0) == (exp_sign > 0):
            continue
        out.append({
            "template_sheet": fc.template_sheet,
            "template_cell": fc.template_cell,
            "metric": fc.metric,
            "value": fc.value,
            "expected": "negative" if exp_sign < 0 else "positive",
            "rule": rule[:160],
            "source_cell": f"{fc.source_sheet}!{fc.source_cell}",
        })
    return out


def violations_to_review_items(violations: list[dict], source_label: str) -> list[dict]:
    """Durable review items for the inbox. Keyed by cell+direction (not value),
    so re-runs re-bind to the same question and an answered one stays answered."""
    from app.review.items import make_item

    items: list[dict] = []
    for v in violations:
        items.append(make_item(
            source="populate", kind="judgment",
            question=(f"{v['template_sheet']}!{v['template_cell']} ({v['metric']}) was filled "
                      f"with a {'positive' if v['value'] > 0 else 'negative'} value but the "
                      f"template's convention says {v['expected']} — is the fill wrong, "
                      "or is the convention noted incorrectly?"),
            why=f"Template rule: {v['rule']}. Last written from {v['source_cell']} ({source_label}).",
            affected={"sheets": [v["template_sheet"]], "cells": [v["template_cell"]],
                      "metrics": [v["metric"]]},
            suggested_answer="fill is wrong — flip the sign",
        ))
    return items
