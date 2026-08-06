"""Connector topology facts: 1-based ref extraction (the deleted passthrough
module's 0-based refs caused the Dec-22 cliff), the multi-input constraint, and
the CX_PUSH entry inventory.
"""

from app.datamodel.topology import (
    extract_refs, is_multi_input, push_entry_summary, push_targets,
)


def test_extract_refs_is_one_based():
    # THE regression: '_PL'!AD20 must be (row 20, col 30) — not (19, 29).
    has_range, refs = extract_refs('=TODECIMAL(IFBLANK(_PL!AD20,""),fxVal_normal)', "PL")
    assert not has_range and refs == {("_PL", 20, 30)}


def test_extract_refs_bare_and_qualified():
    has_range, refs = extract_refs("=IF(K20<>\"Yes\",\"\",Y20)", "PL")
    assert not has_range and refs == {("PL", 20, 11), ("PL", 20, 25)}


def test_extract_refs_flags_ranges_and_rejects_function_names():
    assert extract_refs("=SUM(_PL!AD20:AD50)", "PL")[0] is True
    assert extract_refs("=LOG10(5)", "PL") == (False, set())


def test_is_multi_input_truth_table():
    assert not is_multi_input('=TODECIMAL(IFBLANK(_PL!AD20,""),fxVal_normal)')  # 1-ref wrapper
    assert not is_multi_input('=IFBLANK(Z137,"")')                              # 1-ref relay
    assert not is_multi_input(None) and not is_multi_input("")
    assert is_multi_input("=SUM(AD20:AD50)")             # aggregation fn
    assert is_multi_input("=_PL!AD22/_PL!AD20")          # ratio (2 refs)
    assert is_multi_input("=AVERAGE(AD20:AD41)")         # range function
    assert is_multi_input("=AD20-$L$35")                 # 2 refs
    assert is_multi_input("=MAX(AD20:AD41)")             # range, non-SUM


def test_push_targets_reads_singles_and_ranges():
    cf = {
        ("PL", 50, 100): '=_xldudf_CX_PUSH(pqToday,CX_ENTITY,"Capex",AD50,"As of",W20)',
        ("PL", 51, 100): "=CX_PUSH(x,AD51:AD52)",
        ("PL", 52, 100): '="mentions CX_PUSH in a string"',   # no call shape → no targets
        ("PL", 53, 100): "=SUM(A1:A5)",                        # not a push at all
    }
    t = push_targets(cf)
    assert ("PL", 50, 30) in t and ("PL", 20, 23) in t        # AD50 + W20
    assert ("PL", 51, 30) in t and ("PL", 52, 30) in t        # range expanded
    assert not any(r == 1 for (_s, r, _c) in t)               # no string-mention garbage


def test_push_entry_summary_compacts():
    cf = {("PL", 9, 99): "=CX_PUSH(x,AD20)", ("PL", 10, 99): "=CX_PUSH(x,BN41)"}
    s = push_entry_summary(cf, "PL")
    assert s and "AD" in s and "BN" in s and "20" in s and "41" in s
    assert push_entry_summary(cf, "BS") is None


def test_label_soup_push_contributes_no_single_refs():
    # A push composing labels/conditions references many cells — those are not
    # entry declarations. A tight value-push and any RANGE push still count.
    soup = ("=IF(K20<>\"Yes\",\"\",IF(OR(Y20=\"x\",AB20=\"y\",CM20=1,CN20=2,"
            "_PL!CM20=3),CX_PUSH(a,B20),\"\"))")
    tight = '=_xldudf_CX_PUSH(pqToday,CX_ENTITY,"Capex",AD50,"As of",W20)'
    rng = "=CX_PUSH(x,AD50:BN50)"
    t = push_targets({("PL", 20, 99): soup, ("PL", 50, 99): tight, ("PL", 51, 99): rng})
    assert ("PL", 20, 11) not in t and ("PL", 20, 2) not in t      # soup refs dropped
    assert ("PL", 50, 30) in t and ("PL", 20, 23) in t             # tight push kept
    assert ("PL", 50, 40) in t                                     # range expanded


def test_front_backend_pair_push_declares_the_front():
    # PROD's value-push shape: many refs, but CM20 pairs with _PL!CM20 —
    # the front copy (on the push's own sheet) is the declared entry.
    value_push = ('=IFERROR(IF(CM20*fx=_PL!CM20,"",IF(OR($I20="",$B20=""),'
                  'CX_PUSH(a,CM20),"")),"")')
    t = push_targets({("PL", 20, 103): value_push})
    assert ("PL", 20, 91) in t                       # CM20 front declared
    assert ("PL", 20, 9) not in t and ("PL", 20, 2) not in t   # I20/B20 conditions dropped
