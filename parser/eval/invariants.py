"""Reusable invariants — hand-verified truths about a correct derivation. A
failing invariant is a regression. Each returns an InvariantResult."""

from __future__ import annotations

import re

from eval.harness import InvariantResult

_FILLABLE = ("data", "sourced")
_ROWN = re.compile(r"^row \d+$")
_PROTECTED_ROLES = ("total", "subtotal", "header")


def _fillable(facts):
    return [f for f in facts if f.get("category") in _FILLABLE]


def no_positional_labels(facts) -> InvariantResult:
    """No fillable input may carry a pure positional 'row 25' fallback label — it
    means the metric was never identified (the transposed-grid failure mode)."""
    bad = sorted({f.get("metric_label") for f in _fillable(facts)
                  if _ROWN.match((f.get("metric_label") or ""))})
    return InvariantResult("no_positional_labels", not bad,
                           "" if not bad else f"{len(bad)} 'row N' fillable labels e.g. {bad[:3]}")


def totals_not_fillable(facts) -> InvariantResult:
    """ADVISORY faithfulness signal: a total/subtotal/header row is the template's
    own arithmetic. Populate is already safe (build_demand's value_role guard
    excludes them from what gets written), but the DATA MODEL still labels them
    category 'data' rather than a protected category — so the contract over-states
    what is a required input. Tracked, not gated, until derive is made faithful."""
    bad = [f for f in _fillable(facts)
           if (f.get("value_role") or "").strip().lower() in _PROTECTED_ROLES]
    return InvariantResult("totals_not_fillable", not bad, advisory=True,
                           detail="" if not bad else
                           f"{len(bad)} total/subtotal cells labelled fillable in the model "
                           "(excluded from demand downstream)")


def sourced_have_period(min_fraction: float = 0.8):
    """At least `min_fraction` of connector-fed (sourced) inputs must carry a
    period identity (parsed_date or period_index) — the transposed-grid fix. Only
    graded when the template actually has sourced cells."""
    def _check(facts) -> InvariantResult:
        src = [f for f in facts if f.get("category") == "sourced"]
        if not src:
            return InvariantResult("sourced_have_period", True, "(no sourced cells)")
        withp = [f for f in src if f.get("parsed_date") or f.get("period_index") is not None]
        frac = len(withp) / len(src)
        return InvariantResult("sourced_have_period", frac >= min_fraction,
                               f"{len(withp)}/{len(src)} sourced have a period ({frac:.0%}, floor {min_fraction:.0%})")
    return _check


def labels_excluded(labels: list[str]):
    """Named CONTROL/config labels present in the template must NOT be classified
    as fillable inputs (config pollution regression)."""
    want = {l.strip().lower() for l in labels}
    def _check(facts) -> InvariantResult:
        leaked = sorted({f.get("metric_label") for f in _fillable(facts)
                         if (f.get("metric_label") or "").strip().lower() in want})
        return InvariantResult("controls_excluded", not leaked,
                               "" if not leaked else f"controls leaked as inputs: {leaked}")
    return _check


def labels_fillable(labels: list[str]):
    """Named REAL metric labels, when present, must stay fillable (not swept into
    config) — the ARR-must-not-become-config guard."""
    want = {l.strip().lower() for l in labels}
    def _check(facts) -> InvariantResult:
        present = {(f.get("metric_label") or "").strip().lower(): f.get("category") for f in facts
                   if (f.get("metric_label") or "").strip().lower() in want}
        misclassified = sorted(lbl for lbl, cat in present.items() if cat not in _FILLABLE)
        return InvariantResult("real_metrics_fillable", not misclassified,
                               "" if not misclassified else f"real metrics not fillable: {misclassified}")
    return _check
