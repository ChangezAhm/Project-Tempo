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
    # B5 is deliberately UNCLAIMED: under the write-semantics authority model a
    # CLAIMED single-ref formula becomes a type-over input (see test_type_over),
    # so "plain formula → computed" only holds for unclaimed cells.
    ifields = [input_field("Manual KPI", ["B4"])]
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
# Real corpus — every template CURRENTLY stored with an understanding
# --------------------------------------------------------------------------- #
# The corpus used to be a hardcoded list of 9 template ids. Those templates
# were later deleted from the DB and every real case silently became "No
# versions for template ..." — the regression net was DEAD for weeks while the
# derivation went through four versions unwatched (the whack-a-mole era).
# Anchors are therefore ENUMERATED from the live DB: every stored template
# with an understanding is automatically a regression case, and a new upload
# grows the net instead of aging it. Hand-verified per-template invariants
# key by template id below and simply stop applying if that template goes.
_CHRONOGRAPH = "aeb5f285-3f53-41a0-9d81-3a816390fc49"
_CHRONOGRAPH_CONTROLS = ["POC Mode", "Scenario Selection 1", "Scenario Selection 2",
                         "Scenario Selection 5", "Historical chart series override flags",
                         "KPI Label 1", "KPI Label 5", "KPI Label 10"]
_PER_TEMPLATE_INVARIANTS = {
    _CHRONOGRAPH: [inv.labels_excluded(_CHRONOGRAPH_CONTROLS), inv.labels_fillable(["ARR"])],
}


def real_template_ids() -> list[str]:
    """Templates that exist NOW and have an understanding to derive from."""
    from app import supabase_client as sb
    cl = sb.get_client()
    rows = (cl.table("template_understanding")
            .select("template_version_id, template_versions(template_id)")
            .execute().data or [])
    out: list[str] = []
    for r in rows:
        tid = ((r.get("template_versions") or {}).get("template_id"))
        if tid and tid not in out:
            out.append(tid)
    return sorted(out)


def real_cases() -> list[Case]:
    cases: list[Case] = []
    for tid in real_template_ids():
        invs = [inv.no_positional_labels, inv.totals_not_fillable, inv.sourced_have_period(0.8)]
        invs += _PER_TEMPLATE_INVARIANTS.get(tid, [])
        cases.append(Case(name=tid[:8], template_id=tid, invariants=invs))
    return cases
