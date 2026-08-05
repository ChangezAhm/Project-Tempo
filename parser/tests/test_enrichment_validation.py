"""Offline checks of the enrichment pass's output validation — LLM strings must
be legal enum values before they are persisted as corrections. No API calls.
"""

from app.datamodel.dimensions_llm import DimAssignment, _validated_patch


def _a(**kw):
    base = {"id": 1, "canonical_metric": None, "basis": "unknown", "category": "data"}
    base.update(kw)
    return DimAssignment(**base)


def test_valid_values_pass_through():
    patch = _validated_patch(_a(canonical_metric="revenue", basis="flow", category="config"))
    assert patch == {"canonical_metric": "revenue", "basis": "flow", "category": "config"}


def test_defaults_produce_no_patch_except_data_override():
    # 'unknown' basis is a no-op. category 'data' survives validation since
    # Phase 3 (it can reverse a lexicon-guessed config); the enrich() loop drops
    # it again for metrics with no lexicon call, so plain metrics still produce
    # no correction row.
    assert _validated_patch(_a()) == {"category": "data"}


def test_invalid_basis_and_category_are_dropped():
    # A hallucinated basis, or a category the LLM may not assign ('computed' is
    # derived from formulas), is dropped — not persisted as a garbage correction.
    patch = _validated_patch(_a(canonical_metric="ebitda", basis="quarterly", category="computed"))
    assert patch == {"canonical_metric": "ebitda"}
