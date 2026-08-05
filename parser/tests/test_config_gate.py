"""Config/control/placeholder classification (the data-model faithfulness fix):
selectors, mode toggles, override flags, and empty custom-metric slots are NOT
data inputs and must not generate populate demand. High precision — a real
financial metric is never misclassified."""

from app.datamodel.derive import (
    DERIVATION_VERSION,
    _classify_category,
    _is_control_label,
    _is_junk_label,
    _is_placeholder_label,
)
from app.datamodel.merge import apply_corrections


# --- control selectors / flags -----------------------------------------------
def test_control_labels_detected():
    for lbl in ["POC Mode", "View Mode", "Scenario Selection 1", "Scenario Selection 5",
                "Selected Chronograph Company:", "Historical chart series override flags",
                "Segment II Scenario Selector", "Display Toggle", "Config"]:
        assert _is_control_label(lbl), lbl


# --- placeholder slots -------------------------------------------------------
def test_placeholder_labels_detected():
    for lbl in ["KPI Label 1", "KPI Label 10", "Custom Metric Amount 1", "Custom Metric 3",
                "Metric Label 2", "Segment I Breakdown - Label 4", "[Specify]",
                "[Enter metric]", "Specify", "TBD", "Other…"]:
        assert _is_placeholder_label(lbl), lbl


# --- scaffolding / junk labels (never a data input, even on a connector) -----
def test_junk_labels_detected():
    for lbl in ["L", "F", "Metric Attribute", "Show Unprotected Cell",
                "Please select or type in or leave as blank (3)",
                "Attributes & Data Actions", "Integer", "Date", "Currency", "Decimal",
                "Leverage analysis comment", "Covenant block comments", "$L$1:$BS$52",
                "AD20", "$AD$20"]:
        assert _is_junk_label(lbl), lbl


def test_connector_beats_junk_label_and_is_flagged():
    # Authority model (Fill-Plan Phase 3): the connector formula is a FACT — it
    # self-describes a system-fed input — and a junk-looking label is only a
    # PRIOR. The fact wins; the mismatch is kept visible via the cfg_kind marker
    # (and enrichment/user corrections can still reclassify it).
    cat, kind = _classify_category("Metric Attribute", "=_xldudf_CX_GET(a,b,c)",
                                   None, "input", "PL", set())
    assert cat == "sourced" and kind == "junk_label_connector"


# --- HIGH PRECISION: real financial metrics are never caught -----------------
def test_real_metrics_are_never_classified_config():
    real = [
        "Revenue", "Subscription", "Total Revenue", "Cost of Sales", "Gross Profit",
        "Adjusted EBITDA", "EBITDA", "Net Debt", "Leverage (x)", "Gross Margin %",
        "Headcount (FTE)", "Annual Recurring Revenue", "Net Revenue Retention %",
        "Sales & Marketing", "Depreciation & Amortisation", "Restructuring add-back",
        "Cash & Equivalents", "Senior Debt", "Monthly Churn %", "Revenue per FTE",
        # bare 'Other' lines are real P&L rows — the strict placeholder bank
        # must not silently drop them from populate demand
        "Other", "Others", "Other income",
    ]
    for lbl in real:
        assert not _is_control_label(lbl), f"control FP: {lbl}"
        assert not _is_placeholder_label(lbl), f"placeholder FP: {lbl}"
        assert not _is_junk_label(lbl), f"junk FP: {lbl}"


def test_empty_label_is_neither():
    for lbl in (None, "", "   "):
        assert not _is_control_label(lbl) and not _is_placeholder_label(lbl)


# --- version bump ships the auto-re-derive -----------------------------------
def test_derivation_version_bumped():
    assert DERIVATION_VERSION >= 7


# --- a correction re-opens a config cell as a real input ---------------------
def test_correction_reopens_config_cell():
    facts = [{"id": 1, "sheet_name": "Flash", "metric_label": "Scenario Selection 1",
              "category": "config"}]
    corrections = [{"id": "c1", "match": {"metric_label": "Scenario Selection 1"},
                    "patch": {"category": "data"}}]
    patched, applied, _ = apply_corrections(facts, corrections)
    assert patched[0]["category"] == "data" and "c1" in applied
