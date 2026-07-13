"""Config/control/placeholder classification (the data-model faithfulness fix):
selectors, mode toggles, override flags, and empty custom-metric slots are NOT
data inputs and must not generate populate demand. High precision — a real
financial metric is never misclassified."""

from app.datamodel.derive import (
    DERIVATION_VERSION,
    _is_control_label,
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


# --- HIGH PRECISION: real financial metrics are never caught -----------------
def test_real_metrics_are_never_classified_config():
    real = [
        "Revenue", "Subscription", "Total Revenue", "Cost of Sales", "Gross Profit",
        "Adjusted EBITDA", "EBITDA", "Net Debt", "Leverage (x)", "Gross Margin %",
        "Headcount (FTE)", "Annual Recurring Revenue", "Net Revenue Retention %",
        "Sales & Marketing", "Depreciation & Amortisation", "Restructuring add-back",
        "Cash & Equivalents", "Senior Debt", "Monthly Churn %", "Revenue per FTE",
    ]
    for lbl in real:
        assert not _is_control_label(lbl), f"control FP: {lbl}"
        assert not _is_placeholder_label(lbl), f"placeholder FP: {lbl}"


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
