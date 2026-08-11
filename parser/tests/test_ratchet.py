"""Claims ratchet: a previously committed input claim persists across derives
(monotonic onboarding), bounded by the same structural constraints as live
claims, and facts/memory beat the config label-lexicon."""

from unittest.mock import patch

from eval.builders import build, cell, input_field
from eval.harness import facts_for_inline


def _cell(addr, content):
    if isinstance(content, str) and content.startswith("="):
        return cell(addr, formula=content, value=content, cached="#NAME?")
    return cell(addr, value=content)


def _case(cells, input_fields=(), role="input"):
    return build("PL", [_cell(a, v) for a, v in cells],
                 input_fields=[input_field(f["label"], f["cells"]) for f in input_fields],
                 role=role)


def _by_cell(facts):
    return {f["cell"]: f for f in facts}


FRONT = '=TODECIMAL(IFBLANK(_PL!AD20,""),fxVal_normal)'


def _derive_with_prior(case, prior):
    with patch("app.datamodel.derive._load_prior_claims", return_value=prior):
        return _by_cell(facts_for_inline(*case))


def test_prior_claim_holds_when_this_run_does_not_repeat_it():
    # Run 1 claimed AD20; run 2's understanding forgot it — the ratchet keeps it.
    case = _case(
        cells=[("AD19", "Dec-22"), ("Y20", "Gross Revenue"), ("AD20", FRONT)],
        input_fields=[],  # nothing claimed THIS run
    )
    f = _derive_with_prior(case, {("PL", 20, 30)})["AD20"]
    assert f["category"] == "sourced"
    assert f["write_mode"] == "type_over"
    assert f["category_source"] == "ratchet:prior_claim"


def test_ratchet_never_converts_a_multi_input_formula():
    # The structural constraint outranks memory exactly as it outranks a claim.
    case = _case(
        cells=[("Y20", "Total"), ("AD20", "=SUM(AD10:AD19)")],
        input_fields=[],
    )
    facts = _derive_with_prior(case, {("PL", 20, 30)})
    f = facts.get("AD20")
    assert f is None or f["category"] == "computed"


def test_prior_claimed_cell_missing_from_all_passes_is_rescued():
    # A thinner claim set must not shrink the model: the ratchet pass emits the
    # cell even when no LLM/deterministic/push pass produced it this run.
    case = _case(
        cells=[("AD19", "Dec-22"), ("Y20", "Gross Revenue"), ("AD20", FRONT),
               ("Y21", "Services Revenue"), ("AD21", FRONT)],
        input_fields=[{"label": "Monthly P&L actual", "cells": ["AD20"]}],  # AD21 forgotten
    )
    facts = _derive_with_prior(case, {("PL", 21, 30)})
    assert "AD21" in facts, "prior-claimed cell must re-enter the model"
    assert facts["AD21"]["category"] == "sourced"
    assert facts["AD21"]["category_source"] == "ratchet:prior_claim"


def test_no_prior_no_change():
    # Empty ledger (first derive): unclaimed, unpushed formula never flips.
    case = _case(cells=[("Y20", "Gross Revenue"), ("AD20", FRONT)], input_fields=[])
    facts = _derive_with_prior(case, set())
    f = facts.get("AD20")
    assert f is None or f["category"] == "computed"


def test_prior_claim_beats_placeholder_config_lexicon():
    # A literal cell whose label matches the placeholder lexicon ('KPI 3')
    # classifies config — but a committed prior claim proves it is an entry
    # cell, and facts/memory beat lexicon priors.
    case = _case(
        cells=[("A20", "KPI 3"), ("D20", 125.0)],
        input_fields=[],
    )
    facts = _derive_with_prior(case, {("PL", 20, 4)})
    f = facts.get("D20")
    assert f is not None
    assert f["category"] == "sourced"
    assert f["category_source"] == "ratchet:prior_claim"


def test_fallback_value_cols_recovers_from_sibling_rows():
    # KPI_Dashboard defect: model declared no value_header_cells — the region's
    # own populated rows define the value columns; header dates resolve above.
    from app.authoring.regions import _fallback_value_cols
    cmap = {}
    cmap[(3, 4)] = {"value": "Jan-25"}          # header row above region
    cmap[(3, 5)] = {"value": "Feb-25"}
    for col in (4, 5):
        cmap[(10, col)] = {"value": 100.0}      # existing KPI row (slot)
        cmap[(12, col)] = {"value": None, "formula": "=SUM(D10:D11)"}  # total row
    cols = _fallback_value_cols(cmap, label_col=2, slot_rows=[10, 11],
                                total_row=12, row_start=9)
    assert [c["col"] for c in cols] == [4, 5]
    assert cols[0]["parsed_date"] and cols[0]["parsed_date"].startswith("2025-01")
    assert cols[1]["header_label"] == "Feb-25"
