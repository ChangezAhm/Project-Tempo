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


def test_defaults_produce_no_patch():
    # 'unknown' basis / 'data' category are the no-op defaults, not corrections.
    assert _validated_patch(_a()) == {}


def test_invalid_basis_and_category_are_dropped():
    # A hallucinated basis, or a category the LLM may not assign ('computed' is
    # derived from formulas), is dropped — not persisted as a garbage correction.
    patch = _validated_patch(_a(canonical_metric="ebitda", basis="quarterly", category="computed"))
    assert patch == {"canonical_metric": "ebitda"}
