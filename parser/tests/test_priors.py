"""The consolidated prior lexicons (app/priors.py) come in two strictness tiers:
STRICT banks drive classification with real consequences (data-model category,
region member matching) and must never catch a real business label; LOOSE banks
corroborate/rank only, where a wider net is safe."""

from app.priors import (
    ADJUSTMENT_LEXICON,
    ADJUSTMENT_MEMBER_STRICT,
    ADJUSTMENT_SUBTOTAL,
    is_placeholder_label,
    is_placeholder_slot_label,
)


def test_strict_placeholder_bank_matches_real_slot_names():
    # derive.py-origin examples + regions.py-origin examples
    for lbl in ["Custom KPI #3", "KPI Label 1", "Custom Metric Amount 1",
                "Segment I Breakdown - Label 4", "[Specify]", "TBD", "Other…",
                "Adjustment 3", "Please specify", "Add a KPI", "xxx", "N/A",
                "Other item", "New line 2", "Additional adjustments"]:
        assert is_placeholder_label(lbl), lbl


def test_real_labels_are_not_placeholders():
    # bare 'Other'/'Others'/'New' are real P&L lines far more often than slot
    # names — the STRICT bank (which drops cells from populate demand) must not
    # catch them; only the loose slot tier may.
    for lbl in ["Total Revenue", "Adjusted EBITDA", "Restructuring add-back",
                "Net Revenue Retention %", "Other", "Others", "Other income",
                "New", "Additional", None, "", "   "]:
        assert not is_placeholder_label(lbl), lbl


def test_slot_tier_is_a_loose_superset():
    # region authoring corroborates model claims with structural signals, so it
    # may also treat the bare generic words as placeholder-ish.
    for lbl in ["Other", "Others", "New", "Additional 2", "Custom",
                "KPI Label 1", "[Specify]", "Other…"]:
        assert is_placeholder_slot_label(lbl), lbl
    for lbl in ["Other income", "Total Revenue", "Adjusted EBITDA", None, ""]:
        assert not is_placeholder_slot_label(lbl), lbl


def test_adjustment_lexicon_broad_vs_strict():
    # shared terms both banks catch
    for lbl in ["add-back", "one-off", "restructuring costs",
                "monitoring fee", "write-off", "earn-out", "transaction costs"]:
        assert ADJUSTMENT_LEXICON.search(lbl), lbl
        assert ADJUSTMENT_MEMBER_STRICT.search(lbl), lbl
    # broad-only terms: fine for RANKING (region_bridge orders, never filters)…
    for lbl in ["Transaction fees", "Integration", "stock comp", "pro forma"]:
        assert ADJUSTMENT_LEXICON.search(lbl), lbl
    # …but the STRICT member bank must not call a 'Transaction fees' revenue
    # block an adjustment list (that would make its rows editable).
    for lbl in ["Transaction fees", "Integration", "Adjusted revenue", "Gross Profit"]:
        assert not ADJUSTMENT_MEMBER_STRICT.search(lbl), lbl
    assert not ADJUSTMENT_LEXICON.search("Gross Profit")
    assert ADJUSTMENT_SUBTOTAL.search("EBITDA bridge")
    assert ADJUSTMENT_SUBTOTAL.search("Underlying EBITDA")
    assert not ADJUSTMENT_SUBTOTAL.search("Total Assets")
