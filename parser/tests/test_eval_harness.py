"""The eval harness's CONSTRUCTED golden cases, run as a fast regression gate
(no DB, no LLM). These have authoritative ground truth, so they assert full
precision/recall on the classification pipeline. The 9 real templates run via
`python -m eval` (they need Supabase). See docs/RELIABILITY_AUDIT.md §7."""

import pytest

from eval.corpus import CONSTRUCTED
from eval.harness import run_case


@pytest.mark.parametrize("case", CONSTRUCTED, ids=[c.name for c in CONSTRUCTED])
def test_constructed_case_invariants_and_pr(case):
    r = run_case(case)
    assert r.ran and r.error is None, r.error
    for i in r.invariants:
        assert i.ok, f"{case.name}: invariant {i.name} failed — {i.detail}"
    # constructed cases carry verified ground truth → demand perfect detection
    assert r.precision == 1.0, f"{case.name}: precision {r.precision} — {r.categories}"
    assert r.recall == 1.0, f"{case.name}: recall {r.recall}"
    assert r.category_accuracy == 1.0, f"{case.name}: category accuracy {r.category_accuracy}"


def test_harness_scores_a_deliberate_miss():
    """Guard the scorer itself: a wrong expected set must NOT score 1.0."""
    from eval.harness import score_precision_recall
    facts = [{"sheet_name": "S", "cell": "B2", "category": "config"},
             {"sheet_name": "S", "cell": "B3", "category": "data"}]
    # claim B2 should have been an input (it's config) → recall must drop
    p, r, f1, acc = score_precision_recall(facts, {("S", "B2"): "data", ("S", "B3"): "data"})
    assert r < 1.0 and acc < 1.0
