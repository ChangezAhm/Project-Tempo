"""Sheet-role write gate (WS2) + knowledge feeds (WS6): blank/literal cells on
non-input sheets become category='staging' (never written, correction-overridable);
total/subtotal/header rows are neither cleared nor written; expected_source rides
into the mapper."""

from app.datamodel.derive import DERIVATION_VERSION, _role_blocks_writes
from app.datamodel.merge import apply_corrections


# --- the gate rule ----------------------------------------------------------
def test_role_blocks_writes_matrix():
    surface: set = set()
    for role in ("calc", "lookup", "data_dump", "cover", "instructions", "CALC", "Lookup"):
        assert _role_blocks_writes(role, "S", surface) is True
    for role in ("input", "mixed", None, "", "unknown_role"):
        assert _role_blocks_writes(role, "S", surface) is False   # fail OPEN
    # workbook-level input surface overrides any per-sheet role
    assert _role_blocks_writes("calc", "Inputs", {"Inputs"}) is False


def test_derivation_version_bumped_for_gate():
    # tripwire: the gate ships with an auto-re-derive
    assert DERIVATION_VERSION >= 6


# --- corrections override the gate ------------------------------------------
def test_user_correction_reopens_staging_cell():
    facts = [{"id": 1, "sheet_name": "Calc", "metric_label": "Buffer", "category": "staging"}]
    corrections = [{"id": "c1", "match": {"sheet_name": "Calc"}, "patch": {"category": "data"}}]
    patched, applied, unmatched = apply_corrections(facts, corrections)
    assert patched[0]["category"] == "data" and "c1" in applied and not unmatched
    assert "c1" in patched[0]["applied_correction_ids"]


def test_llm_correction_cannot_reopen_staging():
    # fill-only semantics: an LLM enrichment guess must not override the gate
    facts = [{"id": 1, "sheet_name": "Calc", "metric_label": "Buffer", "category": "staging"}]
    corrections = [{"id": "c2", "created_by": "llm-enrichment",
                    "match": {"sheet_name": "Calc"}, "patch": {"category": "data"}}]
    patched, _, _ = apply_corrections(facts, corrections)
    assert patched[0]["category"] == "staging"


# --- build_demand: staging excluded, totals protected, counts surfaced -------
def _fact(cell, category="data", value_role=None, label="Revenue", pidx=0):
    return {"sheet_name": "P&L", "cell": cell, "row": 5, "col": 3 + (pidx or 0),
            "metric_label": label, "canonical_metric": None, "category": category,
            "value_role": value_role, "period_index": pidx, "period_type": "monthly",
            "scenario": "unknown", "unit": None, "expected_source": "management accounts"}


def test_build_demand_excludes_staging_and_protects_totals(monkeypatch):
    from app.population import run as R
    facts = [
        _fact("C5"),                                            # real input
        _fact("C6", category="staging", label="Scratch"),       # gated
        _fact("C7", value_role="total", label="Total Revenue"), # template's arithmetic
        _fact("C8", category="computed", label="GP"),           # formula
    ]
    monkeypatch.setattr(R, "get_data_model", lambda tid, limit=30000: {
        "available": True, "facts": facts,
        "model": {"dimensions": {"derivation_version": DERIVATION_VERSION},
                  "period_grains": ["monthly"]},
    })
    demand, inputs = R.build_demand("t1", None)
    cells = {f["cell"] for f in inputs}
    assert cells == {"C5"}                       # staging + total + computed all out
    assert demand["gated_cells"] == 1
    assert demand["protected_totals"] == 1
    # expected_source rides into the demand metric
    assert demand["metrics"][0]["expected_source"] == "management accounts"


def test_timeseries_excludes_staging(monkeypatch):
    from app.datamodel import persist
    facts = [
        {**_fact("C5"), "parsed_date": "2026-01-31", "period_label": "Jan-26",
         "basis": "flow", "definition": None},
        {**_fact("C6", category="staging", label="Scratch"), "parsed_date": "2026-01-31",
         "period_label": "Jan-26", "basis": "flow", "definition": None},
    ]
    monkeypatch.setattr(persist, "get_data_model",
                        lambda tid, limit=30000: {"available": True,
                                                  "template_version_id": "v1", "facts": facts})
    out = persist.timeseries_view("t1")
    labels = {m["label"] for sh in out["sheets"] for m in sh["metrics"]}
    assert "Revenue" in labels and "Scratch" not in labels


# --- binding defense-in-depth + mapper feed ----------------------------------
def test_bind_never_writes_total_rows():
    from planpath import bind
    from app.population.schema import MetricMap
    fact = {"sheet_name": "T", "cell": "B5", "canonical_metric": "revenue",
            "metric_label": "Total Revenue", "value_role": "subtotal",
            "period_index": 0, "scenario": "unknown", "parsed_date": None,
            "unit": None, "currency": None}
    maps = [MetricMap(metric="revenue", series_id="X!r1", confidence=0.9)]
    links, unmatched = bind([fact], {}, maps, {"period_count": 1, "period_grain": "monthly",
                                               "metrics": []})
    assert not links
    assert "never written" in unmatched[0]["reason"]


def test_mapper_lines_render_expected_source():
    from app.population.mapping import _metric_lines
    line = _metric_lines([{"metric": "revenue", "label": "Revenue",
                           "expected_source": "management accounts"}])
    assert "source: management accounts" in line
