"""Defaulted-input (passthrough) detection — the structural safety invariant and
the real shapes seen on the Flash PROD template (front → _PL connector, subtotals,
sign helpers, multi-hop display columns)."""

from app.datamodel.passthrough import (
    build_data_cols,
    extract_refs,
    find_passthrough_inputs,
    resolve_passthrough,
)


def _cf(**cells):
    """Helper: build a cell_formula map from {'PL!AD20': '=...'} style keys."""
    out = {}
    for k, v in cells.items():
        sheet, a1 = k.split("!")
        import re
        m = re.match(r"([A-Z]+)(\d+)", a1)
        col = 0
        for ch in m.group(1):
            col = col * 26 + (ord(ch) - 64)
        out[(sheet, int(m.group(2)) - 1, col - 1)] = v
    return out


# ---- reference extraction --------------------------------------------------

def test_extract_refs_ignores_named_ranges_and_functions():
    has_range, refs = extract_refs('=TODECIMAL(IFBLANK(_PL!AD20,""),fxVal_normal)', "PL")
    assert has_range is False
    assert refs == {("_PL", 19, 29)}          # only _PL!AD20; fxVal_normal excluded


def test_extract_refs_flags_ranges():
    has_range, refs = extract_refs("=SUM(_PL!AD20:AD50)", "PL")
    assert has_range is True and refs == set()


def test_extract_refs_counts_multiple_operands():
    _, refs = extract_refs("=PL!AD20/PL!AD30", "PL")
    assert len(refs) == 2                       # a ratio is never a passthrough


def test_extract_refs_rejects_function_names_that_look_cellish():
    # LOG10( and ATAN2( must not be read as cells
    _, refs = extract_refs("=LOG10(AD20)", "PL")
    assert refs == {("PL", 19, 29)}             # AD20 only, not LOG10


# ---- the core safety invariant ---------------------------------------------

def test_single_passthrough_to_connector_is_flagged():
    cf = _cf(**{"PL!AD20": '=TODECIMAL(IFBLANK(_PL!AD20,""),fxVal)',
                "_PL!AD20": "=_xldudf_CX_GET(CX_ENTITY,$F20,AD11,AD14,,AD19)"})
    inputs, backing = find_passthrough_inputs(["PL"], cf, {})
    assert ("PL", 19, 29) in inputs             # front cell is the input
    assert ("_PL", 19, 29) not in backing       # a connector is never suppressed (connector pass owns it)


def test_aggregate_over_connectors_is_not_flagged():
    cf = _cf(**{"PL!AD99": "=SUM(_PL!AD20:AD50)",
                "_PL!AD20": "=CX_GET(a,b,c)", "_PL!AD50": "=CX_GET(a,b,c)"})
    inputs, _ = find_passthrough_inputs(["PL"], cf, {})
    assert ("PL", 98, 29) not in inputs         # a total is never an input


def test_ratio_of_connectors_is_not_flagged():
    cf = _cf(**{"PL!AD99": "=_PL!AD20/_PL!AD30",
                "_PL!AD20": "=CX_GET(a,b,c)", "_PL!AD30": "=CX_GET(a,b,c)"})
    inputs, _ = find_passthrough_inputs(["PL"], cf, {})
    assert ("PL", 98, 29) not in inputs


def test_sign_helper_into_non_data_column_is_not_flagged():
    # Z35 = SWITCH(TRUE,_PL!$L35=TRUE,1,...) — L is a boolean-flag column, not data.
    cf = _cf(**{"PL!Z35": "=SWITCH(TRUE,_PL!L35=TRUE,1,_PL!L35=FALSE,-1,0)",
                "_PL!AD35": "=CX_GET(a,b,c)"})          # data col = AD, not L
    inputs, _ = find_passthrough_inputs(["PL"], cf, {})
    assert not inputs                            # L35 isn't an input point → nothing


# ---- multi-hop resolution + dedupe -----------------------------------------

def test_two_hop_display_resolves_to_front_only():
    # J137 = IFBLANK(Z137,"");  Z137 = _PL!Z137 (connector). Only J137 survives.
    cf = _cf(**{"PL!Z137": "=_PL!Z137",
                "PL!J137": '=IFBLANK(Z137,"")',
                "_PL!Z137": "=CX_GET(a,b,c)"})
    inputs, backing = find_passthrough_inputs(["PL"], cf, {})
    assert ("PL", 136, 9) in inputs             # J137 (outermost) is the input
    assert ("PL", 136, 25) not in inputs        # Z137 intermediate suppressed
    assert ("PL", 136, 25) in backing


def test_blank_in_data_column_is_an_input_point():
    cf = _cf(**{"PL!AD40": '=IFBLANK(_PL!AD40,"")',
                "_PL!AC40": "=CX_GET(a,b,c)"})   # AC is a connector → AD is a data col? no
    # give _PL a connector in column AD so AD is a data column, target is blank there
    cf[("_PL", 10, 29)] = "=CX_GET(a,b,c)"       # _PL!AD11 connector → AD in data_cols
    inputs, _ = find_passthrough_inputs(["PL"], cf, {("_PL", 39, 29): None})
    assert ("PL", 39, 29) in inputs             # blank _PL!AD40 in a data col is an input


def test_key_that_is_itself_a_connector_is_left_to_the_connector_pass():
    cf = _cf(**{"KPI!AI34": "=IFERROR(_xldudf_CX_GET(a,b,c),\"\")"})
    inputs, _ = find_passthrough_inputs(["KPI"], cf, {})
    assert not inputs                            # not our job; connector pass owns it


def test_cycle_is_safe():
    cf = _cf(**{"PL!A1": "=A2", "PL!A2": "=A1"})
    assert resolve_passthrough(("PL", 0, 0), cf, {}, build_data_cols(cf)) is None


def test_other_sheets_are_not_scanned():
    cf = _cf(**{"PL!AD20": "=_PL!AD20", "_PL!AD20": "=CX_GET(a,b,c)"})
    inputs, _ = find_passthrough_inputs(["BS"], cf, {})   # only BS requested
    assert not inputs
