"""Write-semantics inversion: the understanding's claim (or the workbook's own
CX_PUSH topology) makes a formula cell a TYPE-OVER default — bounded by the one
structural constraint (a multi-input formula is never converted). Ports the
semantic invariants of the deleted passthrough tests to the real derive path.
"""

from eval.builders import build, cell, input_field
from eval.harness import facts_for_inline


def _cell(addr, content):
    """A test cell: '=' content is a formula (value mirrors the parser's
    formula-text convention); anything else is a literal."""
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


def test_claimed_single_ref_front_becomes_type_over():
    # The PROD shape: a claimed FX-wrapper front over a backend connector.
    case = _case(
        cells=[("AD19", "Dec-22"), ("Y20", "Gross Revenue"), ("AD20", FRONT)],
        input_fields=[{"label": "Monthly P&L actual", "cells": ["AD20"]}],
    )
    f = _by_cell(facts_for_inline(*case))["AD20"]
    assert f["category"] == "sourced"
    assert f["write_mode"] == "type_over"
    assert f["category_source"] == "llm:input_field"


def test_subtotal_inside_claimed_range_stays_computed():
    # Ported invariant: an aggregate is NEVER an input, whatever the claim.
    case = _case(
        cells=[("Y20", "Rev A"), ("AD20", FRONT),
               ("Y21", "Rev B"), ("AD21", FRONT.replace("AD20", "AD21")),
               ("Y22", "Total"), ("AD22", "=SUBTOTAL(9,AD20:AD21)")],
        input_fields=[{"label": "P&L block", "cells": ["AD20:AD22"]}],
    )
    by = _by_cell(facts_for_inline(*case))
    assert by["AD20"]["write_mode"] == "type_over"
    assert by["AD22"]["category"] == "computed" and by["AD22"]["write_mode"] is None


def test_ratio_stays_computed_even_when_claimed():
    # Ported invariant: a ratio/multi-operand formula is never an input.
    # (_is_aggregation alone passes '=A/B' — is_multi_input is the tripwire.)
    case = _case(
        cells=[("Y23", "Margin %"), ("AD23", "=_PL!AD22/_PL!AD20")],
        input_fields=[{"label": "margin", "cells": ["AD23"]}],
    )
    f = _by_cell(facts_for_inline(*case))["AD23"]
    assert f["category"] == "computed" and f["write_mode"] is None


def test_range_function_stays_computed_even_when_claimed():
    case = _case(
        cells=[("Y24", "Avg revenue"), ("AD24", "=AVERAGE(AD20:AD23)")],
        input_fields=[{"label": "avg", "cells": ["AD24"]}],
    )
    f = _by_cell(facts_for_inline(*case))["AD24"]
    assert f["category"] == "computed" and f["write_mode"] is None


def test_display_chain_claim_selects_the_write_target():
    # Ported invariant (restated): the CLAIM selects the write target; the
    # unclaimed relay link stays computed.
    case = _case(
        cells=[("Y30", "Net Debt"), ("J30", '=IFBLANK(Z30,"")'), ("Z30", "=_PL!Z30")],
        input_fields=[{"label": "net debt", "cells": ["J30"]}],
    )
    by = _by_cell(facts_for_inline(*case))
    assert by["J30"]["write_mode"] == "type_over"
    assert "Z30" not in by or by["Z30"]["category"] == "computed"


def test_unclaimed_unpushed_formula_never_flips():
    case = _case(cells=[("Y31", "Helper"), ("AD31", "=_PL!AD31")])
    by = _by_cell(facts_for_inline(*case))
    assert "AD31" not in by or by["AD31"]["category"] == "computed"


def test_claimed_connector_with_blank_cache_is_sourced():
    # Blank-cached connectors used to be dropped by pass 3's numeric guard —
    # a CLAIM has no such guard (the blank-connector KPI recall fix).
    case = _case(
        cells=[("Y40", "ARR"), ("AD40", '=_xldudf_CX_GET(CX_ENTITY,"arr","2026-01","M")')],
        input_fields=[{"label": "ARR", "cells": ["AD40"]}],
    )
    f = _by_cell(facts_for_inline(*case))["AD40"]
    assert f["category"] == "sourced"


def test_push_read_cells_become_inputs_without_a_claim():
    # The workbook pushes what you type there — entry cells by its own declaration.
    case = _case(
        cells=[("Y50", "Capex"), ("AD50", FRONT.replace("AD20", "AD50")),
               ("CY50", '=_xldudf_CX_PUSH(pqToday,CX_ENTITY,"Capex",AD50,"As of",W20)')],
    )
    by = _by_cell(facts_for_inline(*case))
    f = by["AD50"]
    assert f["category"] == "sourced" and f["write_mode"] == "type_over"
    assert f["category_source"] == "topology:push"


def test_push_never_converts_a_multi_input_formula():
    case = _case(
        cells=[("Y51", "Total"), ("AD51", "=SUM(AD40:AD50)"),
               ("CY51", '=_xldudf_CX_PUSH(pqToday,CX_ENTITY,"Total",AD51,"As of",W20)')],
    )
    by = _by_cell(facts_for_inline(*case))
    assert "AD51" not in by or by["AD51"]["category"] == "computed"


def test_hidden_row_stays_staging_even_when_claimed():
    # Hidden geometry is the author's own "not user-facing" declaration — it
    # beats a writable classification (stamped, user-correctable).
    und, structure, snap = _case(
        cells=[("Y60", "Backend mirror"), ("AD60", "5")],
        input_fields=[{"label": "mirror", "cells": ["AD60"]}],
    )
    snap["sheets"][0]["hidden_rows"] = [60]
    f = _by_cell(facts_for_inline(und, structure, snap))["AD60"]
    assert f["category"] == "staging" and f["category_source"] == "geometry:hidden"


def test_metric_row_input_claim_covers_unenumerated_rows():
    # Task #12 regression: a metric_row the understanding marks value_role='input'
    # is a row-level claim — its cells become facts across the period columns
    # even when input_fields never enumerated the row (the flash Budget rows).
    und, structure, snap = _case(
        cells=[("B4", "hdr"), ("C4", "Jan-26"), ("D4", "Feb-26"),
               ("B31", "Depreciation"), ("C31", "10"), ("D31", "11"),
               ("B32", "Budget"), ("C32", None), ("D32", None)],
        input_fields=[{"label": "Depreciation (actual)", "cells": ["C31:D31"]}],
    )
    u = und["sheets"][0]["understanding"]
    u["periods"] = [{"header_cell": "C4", "date": "2026-01-31", "granularity": "monthly"},
                    {"header_cell": "D4", "date": "2026-02-28", "granularity": "monthly"}]
    u["metric_rows"] = [
        {"label_cell": "B31", "label": "Depreciation", "value_role": "input", "scenario": "actual"},
        {"label_cell": "B32", "label": "Depreciation (Budget)", "value_role": "input", "scenario": "budget"},
    ]
    by = _by_cell(facts_for_inline(und, structure, snap))
    assert "C32" in by and "D32" in by            # the Budget row now has facts
    assert by["C32"]["category"] == "data"
