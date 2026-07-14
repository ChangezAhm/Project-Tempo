"""The golden corpus.

CONSTRUCTED cases have authoritative ground truth (we built them) → full
precision/recall; they're fast (no DB) and run in the pytest suite as a hard
regression gate on the classification pipeline.

REAL cases run the deterministic derivation over the 9 stored templates →
invariants + a category-distribution baseline. They need DB access, so they run
via the CLI (`python -m eval`), not the default pytest.
"""

from __future__ import annotations

from eval import invariants as inv
from eval.builders import build, cell, input_field
from eval.harness import Case

# --------------------------------------------------------------------------- #
# Constructed golden cases (full ground truth)
# --------------------------------------------------------------------------- #


def _config_case() -> Case:
    """Controls must be excluded; real metrics must stay fillable."""
    cells = [
        cell("A2", value="POC Mode"),            cell("B2", value="Actual"),
        cell("A3", value="Scenario Selection 1"), cell("B3", value="Budget"),
        cell("A4", value="Revenue"),              cell("B4", value=100.0),
        cell("A5", value="Adjusted EBITDA"),      cell("B5", value=42.0),
    ]
    ifields = [input_field(l, [c]) for l, c in
               [("POC Mode", "B2"), ("Scenario Selection 1", "B3"),
                ("Revenue", "B4"), ("Adjusted EBITDA", "B5")]]
    und = build("Flash", cells, input_fields=ifields)
    expected = {("Flash", "B2"): "config", ("Flash", "B3"): "config",
                ("Flash", "B4"): "data", ("Flash", "B5"): "data"}
    return Case("constructed:config-pollution", inline=und, expected=expected,
                invariants=[inv.labels_excluded(["POC Mode", "Scenario Selection 1"]),
                            inv.labels_fillable(["Revenue", "Adjusted EBITDA"])],
                description="control/selector cells excluded; real metrics stay fillable")


def _connector_case() -> Case:
    """A connector-fed FINANCIAL cell is a replaceable input (sourced); a
    connector STATUS cell is not; a plain formula is computed; a literal is data."""
    fin = '=IF("CX.UNLINK"="CX.UNLINK",95.76,IFERROR(CX_GET(CX_E,"Revenue"),0))'
    status = '=IF("CX.UNLINK"="CX.UNLINK",0,IFERROR(CX_GET(CX_E,"Status"),0))'
    cells = [
        cell("A2", value="Revenue"),    cell("B2", formula=fin, cached=95.76),
        cell("A3", value="RAG"),        cell("B3", formula=status, cached="GREEN"),
        cell("A4", value="Manual KPI"), cell("B4", value=12.0),
        cell("A5", value="Growth"),     cell("B5", formula="=B2*2", cached=191.0),
    ]
    ifields = [input_field("Manual KPI", ["B4"]), input_field("Growth", ["B5"])]
    und = build("Flash", cells, input_fields=ifields)
    # B3 (status) is not emitted at all → absent from expected inputs.
    expected = {("Flash", "B2"): "sourced", ("Flash", "B4"): "data",
                ("Flash", "B5"): "computed"}
    return Case("constructed:connector-inputs", inline=und, expected=expected,
                invariants=[inv.sourced_have_period(0.0)],
                description="connector financial→sourced, status→excluded, formula→computed, literal→data")


def _transposed_case() -> Case:
    """A transposed connector grid (period down the rows, metric across columns)
    resolves metric + period from the CX_GET arguments."""
    cx = '=IF("CX.UNLINK"="CX.UNLINK",95.76,IFERROR(_xldudf_CX_GET(CX_E,\'Flash\'!G$1,$F2,"Month",,0),0))'
    cells = [
        cell("G1", value="Net Revenue"),
        cell("F2", cached="2023-11-30T00:00:00"),
        cell("G2", formula=cx, cached=95.76),
    ]
    und = build("Flash", cells, role="calc", input_surface=[])
    expected = {("Flash", "G2"): "sourced"}
    return Case("constructed:transposed-grid", inline=und, expected=expected,
                invariants=[inv.no_positional_labels, inv.sourced_have_period(0.9)],
                description="transposed connector grid resolves metric+period from CX_GET args")


CONSTRUCTED: list[Case] = [_config_case(), _connector_case(), _transposed_case()]


# --------------------------------------------------------------------------- #
# Real corpus — the 9 stored templates
# --------------------------------------------------------------------------- #
_CHRONOGRAPH = "aeb5f285-3f53-41a0-9d81-3a816390fc49"
REAL_TEMPLATE_IDS = [
    _CHRONOGRAPH,
    "d9c1589d-c60a-4146-8726-2086d3a82ac8",
    "5a6e9620-bc45-4483-984a-5b04b7fd7c08",
    "6c047599-a508-4c86-98c1-2dce138d996a",
    "d7f0f4ac-bced-4b22-9a41-0edcc24ad4da",
    "38d240a2-2f11-4ccb-ae92-b1b34b04985c",
    "e8d562f4-7a8f-4da3-a380-0ea110b701ee",
    "787d2d51-5cc7-4ee6-b1f2-32fcb3add93e",
    "3ee0051b-07b9-4d04-90a6-867dbb11d750",
]

# Hand-verified truths for the Chronograph flash (this session's fixes).
_CHRONOGRAPH_CONTROLS = ["POC Mode", "Scenario Selection 1", "Scenario Selection 2",
                         "Scenario Selection 5", "Historical chart series override flags",
                         "KPI Label 1", "KPI Label 5", "KPI Label 10"]


def real_cases() -> list[Case]:
    cases: list[Case] = []
    for tid in REAL_TEMPLATE_IDS:
        invs = [inv.no_positional_labels, inv.totals_not_fillable, inv.sourced_have_period(0.8)]
        if tid == _CHRONOGRAPH:
            invs += [inv.labels_excluded(_CHRONOGRAPH_CONTROLS), inv.labels_fillable(["ARR"])]
        cases.append(Case(name=tid[:8], template_id=tid, invariants=invs))
    return cases
