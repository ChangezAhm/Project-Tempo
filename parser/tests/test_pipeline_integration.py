"""Orchestration integration tests: the WHOLE population pipeline offline —
LLM entry points and storage monkeypatched, everything between them real
(reconcile, catalogue, verify, execute, apply, render via Aspose). This is the
coverage the build audit flagged as the most dangerous hole: correct pieces
interacting incorrectly is exactly what unit tests can't see."""

from __future__ import annotations

import gzip
import json

import pytest

import app.supabase_client as sb
from app.datamodel.derive import DERIVATION_VERSION
from app.population.cost import SpendCapExceeded
from app.population.schema import MetricMap

# ---- fixtures ----------------------------------------------------------------

_DATES = [("C", "2024-01-31"), ("D", "2024-02-29"),
          ("E", "2024-03-31"), ("F", "2024-04-30")]


def _tpl_facts():
    """The template data model's input facts: Revenue across 4 monthly slots."""
    return [{"sheet_name": "PL", "cell": f"{c}5", "row": 5, "col": i + 3,
             "metric_label": "Revenue", "canonical_metric": None, "category": "data",
             "value_role": None, "period_index": i, "period_type": "monthly",
             "scenario": "unknown", "unit": "GBP", "parsed_date": d,
             "expected_source": None}
            for i, (c, d) in enumerate(_DATES)]


def _tpl_snapshot():
    cells = [{"address": f"{c}4", "row": 4, "col": i + 3, "value": d}
             for i, (c, d) in enumerate(_DATES)]
    cells.append({"address": "B5", "row": 5, "col": 2, "value": "Revenue"})
    return {"sheets": [{"name": "PL", "cells": cells}]}


def _src_snapshot():
    cells = [{"address": f"{c}4", "row": 4, "col": i + 3, "value": d}
             for i, (c, d) in enumerate(_DATES)]
    cells.append({"address": "B5", "row": 5, "col": 2, "value": "Revenue"})
    for i, (c, _d) in enumerate(_DATES):
        cells.append({"address": f"{c}5", "row": 5, "col": i + 3,
                      "value": 100.0 + 10 * i})
    return {"sheets": [{"name": "SRC", "cells": cells}]}


def _src_claims():
    return [{"sheet": "SRC",
             "periods": [{"header_cell": f"{c}4", "date": d, "grain": "month",
                          "kind": "actual"} for c, d in _DATES],
             "series": [{"label_cell": "B5", "label": "Revenue"}]}]


def _make_template_bytes(tmp_path, *, with_checks: bool = False) -> bytes:
    from aspose.cells import Workbook
    wb = Workbook()
    ws = wb.worksheets[0]
    ws.name = "PL"
    ws.cells.get("B5").put_value("Revenue")
    for i, (c, d) in enumerate(_DATES):
        ws.cells.get(f"{c}4").put_value(d)
    if with_checks:
        chk = wb.worksheets.add("Checks")
        chk.cells.get("B1").formula = "=PL!C5-999"   # ties only if C5 == 999
    p = tmp_path / "tpl.xlsx"
    wb.save(str(p))
    return p.read_bytes()


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Offline world: storage + DB + LLM faked, calls recorded."""
    calls = {"cache_puts": [], "uploads": [], "questions": None,
             "map_metrics": 0, "understand_and_map": 0, "understand_source": 0,
             "revise_for_checks": 0}
    facts = _tpl_facts()

    import app.population.pipeline.demand as demand_mod
    monkeypatch.setattr(demand_mod, "get_data_model", lambda tid, limit=30000: {
        "available": True, "facts": facts,
        "model": {"fact_count": len(facts),
                  "dimensions": {"derivation_version": DERIVATION_VERSION},
                  "period_grains": ["monthly"]}})

    tpl_bytes = _make_template_bytes(tmp_path)
    monkeypatch.setattr(sb, "get_latest_file", lambda tid: ("vid1", "p", "tpl.xlsx"))
    monkeypatch.setattr(sb, "download_workbook", lambda path: tpl_bytes)
    monkeypatch.setattr(sb, "download_snapshot",
                        lambda vid: gzip.compress(json.dumps(_tpl_snapshot()).encode()))
    monkeypatch.setattr(sb, "upload_filled",
                        lambda vid, label, data, variant=None, run_stamp=None:
                        calls["uploads"].append(("filled", variant)) or f"path/{variant}")
    monkeypatch.setattr(sb, "upload_audit",
                        lambda vid, label, data, run_stamp=None:
                        calls["uploads"].append(("audit", None)) or "path/audit")
    monkeypatch.setattr(sb, "signed_filled_url", lambda path: f"https://signed/{path}")
    monkeypatch.setattr(sb, "list_extensible_regions", lambda vid: [])

    import app.population.pipeline.prepare as prepare_mod
    monkeypatch.setattr(prepare_mod, "load_context", lambda tid, vid: "")
    import app.understanding.persist as und_persist
    monkeypatch.setattr(und_persist, "get_understanding", lambda tid: None)
    import app.understanding.sheet_image as sheet_image
    monkeypatch.setattr(sheet_image, "render_sheet_tiles",
                        lambda *a, **k: [], raising=False)

    import app.population.contract as contract
    monkeypatch.setattr(contract, "load_decisions", lambda vid, fingerprint=None: {})

    import app.review.items as review_items
    monkeypatch.setattr(review_items, "file_questions",
                        lambda vid, items, family, cap=None:
                        calls.__setitem__("questions", list(items)) or {"filed": len(items)})

    import app.population.source_cache as source_cache
    monkeypatch.setattr(source_cache, "get", lambda key: None)
    monkeypatch.setattr(source_cache, "put",
                        lambda key, val: calls["cache_puts"].append((key, val.get("stage"))))

    import app.population.pipeline.understand as und_mod
    monkeypatch.setattr(und_mod, "cached_sheets", lambda ch: None)

    def fake_understand_source(snapshot, content_hash, source_path=None):
        calls["understand_source"] += 1
        return _src_claims()
    monkeypatch.setattr(und_mod, "understand_source", fake_understand_source)

    import app.population.pipeline.plan as plan_mod

    def fake_map_metrics(metrics, catalogue, context="", grids=None):
        calls["map_metrics"] += 1
        sid = next(iter(catalogue))
        return ([MetricMap(metric=m["metric"], series_id=sid, confidence=0.9,
                           status="direct", source_unit="GBP") for m in metrics], [])
    monkeypatch.setattr(plan_mod, "map_metrics", fake_map_metrics)

    import app.population.mapping as mapping
    monkeypatch.setattr(mapping, "revise_plan", lambda *a, **k: [])

    def fake_understand_and_map(metrics, grids, context="", images=None):
        calls["understand_and_map"] += 1
        raw = [{"metric": m["metric"], "source": "SRC!B5", "confidence": 0.9,
                "source_unit": "GBP"} for m in metrics]
        return _src_claims(), raw, False
    monkeypatch.setattr(mapping, "understand_and_map", fake_understand_and_map)

    return calls


def _run(**kw):
    from app.population.run import _run_population
    defaults = dict(target_template_id="t1", source_snapshot=_src_snapshot(),
                    source_periods={}, source_label="pack.xlsx", as_of_date=None,
                    content_hash=None, link_sources=False)
    defaults.update(kw)
    return _run_population(defaults.pop("target_template_id"),
                           defaults.pop("source_snapshot"),
                           defaults.pop("source_periods"),
                           defaults.pop("source_label"),
                           defaults.pop("as_of_date"), **defaults)


# ---- the wiring tests --------------------------------------------------------

def test_digest_path_end_to_end(harness, monkeypatch):
    monkeypatch.setenv("TEMPO_MAPPER", "digest")
    res = _run()
    assert res["routing"]["mapper"] == "digest"
    assert res["routing"]["catalogue_source"] == "ai_understanding"
    assert harness["understand_source"] == 1 and harness["map_metrics"] == 1
    assert harness["understand_and_map"] == 0
    filled = {(f["template_sheet"], f["template_cell"]): f["value"] for f in res["filled"]}
    assert filled == {("PL", "C5"): 100.0, ("PL", "D5"): 110.0,
                      ("PL", "E5"): 120.0, ("PL", "F5"): 130.0}
    assert res["filled_url"] and res["audit_url"]
    assert ("audit", None) in harness["uploads"]
    assert harness["questions"] is not None   # filing gate ran exactly once


def test_grid_onepass_end_to_end(harness, monkeypatch):
    monkeypatch.setenv("TEMPO_MAPPER", "grid")
    res = _run()
    assert res["routing"]["mapper"] == "grid-onepass"
    assert harness["understand_and_map"] == 1
    assert harness["map_metrics"] == 0        # one-pass maps; batched mapper never runs
    assert len(res["filled"]) == 4
    assert res["summary"]["filled"] == 4 if isinstance(res["summary"], dict) else True


def test_onepass_failure_falls_back_to_two_pass(harness, monkeypatch):
    monkeypatch.setenv("TEMPO_MAPPER", "grid")
    import app.population.mapping as mapping

    def boom(*a, **k):
        harness["understand_and_map"] += 1
        raise RuntimeError("model returned garbage")
    monkeypatch.setattr(mapping, "understand_and_map", boom)
    res = _run()
    # loud fallback: the two-pass digest chain still delivers the fill
    assert harness["understand_and_map"] == 1
    assert harness["understand_source"] == 1 and harness["map_metrics"] == 1
    assert res["routing"]["mapper"] == "grid"
    assert len(res["filled"]) == 4


def test_plan_cache_final_skips_every_llm_stage(harness, monkeypatch):
    monkeypatch.setenv("TEMPO_MAPPER", "grid")
    import app.population.pipeline.understand as und_mod
    import app.population.source_cache as source_cache
    monkeypatch.setattr(und_mod, "cached_sheets", lambda ch: _src_claims())
    maps = [{"metric": "Revenue", "series_id": "SRC!r5", "confidence": 0.9,
             "status": "direct", "source_unit": "GBP"}]
    monkeypatch.setattr(source_cache, "get",
                        lambda key: {"stage": "final", "maps": maps})
    res = _run(content_hash="abc123")
    assert res["routing"]["catalogue_source"] == "plan-cache:final"
    assert harness["understand_and_map"] == 0
    assert harness["map_metrics"] == 0
    assert harness["understand_source"] == 0
    assert len(res["filled"]) == 4
    # the final plan is re-persisted (still stage 'final'), never 'mapped'
    assert all(stage == "final" for _k, stage in harness["cache_puts"])


def test_fresh_mapping_writes_mapped_then_final_cache(harness, monkeypatch):
    monkeypatch.setenv("TEMPO_MAPPER", "digest")
    res = _run(content_hash="abc123")
    stages = [stage for _k, stage in harness["cache_puts"]]
    assert stages == ["mapped", "final"]      # abort insurance, then the run's plan
    keys = {k for k, _s in harness["cache_puts"]}
    assert len(keys) == 1                     # both writes under THE plan key
    from app.population.pipeline.understand import plan_key_for
    assert keys == {plan_key_for("abc123", "vid1")}
    assert len(res["filled"]) == 4


def test_torn_data_model_refuses_to_populate(harness, monkeypatch):
    import app.population.pipeline.demand as demand_mod
    facts = _tpl_facts()[:1]                  # 1 stored fact vs 4 declared = torn
    monkeypatch.setattr(demand_mod, "get_data_model", lambda tid, limit=30000: {
        "available": True, "facts": facts,
        "model": {"fact_count": 4,
                  "dimensions": {"derivation_version": DERIVATION_VERSION},
                  "period_grains": ["monthly"]}})
    monkeypatch.setattr(demand_mod, "derive_and_persist",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError, match="incomplete"):
        _run()
    assert harness["map_metrics"] == 0 and harness["understand_and_map"] == 0


def test_dry_run_estimates_without_spending(harness, monkeypatch):
    import app.population.pipeline.estimate as est_mod
    monkeypatch.setattr(est_mod, "cached_sheets", lambda ch: None)
    monkeypatch.setattr(est_mod, "estimate_source_understanding_usd", lambda snap: 2.5)
    monkeypatch.setattr(est_mod, "load_context", lambda tid, vid: "")
    res = _run(dry_run=True)
    assert res["dry_run"] is True
    assert res["source_understanding"] == "would_run"
    assert res["estimated_total_usd"] == 5.5          # 2.5 + 3.0 loops allowance
    assert res["cap_exceeded"] is False
    assert res["reset_preview"]["cells_in_scope"] == 4
    assert harness["map_metrics"] == 0 and harness["understand_and_map"] == 0
    assert harness["uploads"] == []                   # nothing rendered or stored


def test_tieout_probe_triggers_check_revision(harness, monkeypatch, tmp_path):
    monkeypatch.setenv("TEMPO_MAPPER", "grid")
    tpl_bytes = _make_template_bytes(tmp_path, with_checks=True)
    monkeypatch.setattr(sb, "download_workbook", lambda path: tpl_bytes)
    import app.population.template_checks as tc
    monkeypatch.setattr(tc, "collect_check_cells",
                        lambda snap, und=None, **kw: [
                            {"sheet": "Checks", "cell": "B1", "row": 1, "col": 2,
                             "kind": "tie_out", "origin": "test",
                             "label": "Checks!B1", "before": None}])
    import app.population.mapping as mapping

    def fake_revise_for_checks(failures, metric_maps, catalogue, grids,
                               context="", flags=None):
        harness["revise_for_checks"] += 1
        assert failures and failures[0]["cell"] == "B1"
        return [MetricMap(metric="Revenue", series_id=next(iter(catalogue)),
                          confidence=0.95, status="direct", source_unit="GBP")]
    monkeypatch.setattr(mapping, "revise_for_checks", fake_revise_for_checks)
    res = _run()
    # probe found the failing tie-out (C5=100 ≠ 999) BEFORE any deliverable,
    # the revision ran, and the final verdict still reports the failure loudly
    assert harness["revise_for_checks"] == 1
    assert res["routing"]["tie_out"]["failed_before"] >= 1
    assert res["routing"]["tie_out"]["revised"] == 1
    assert res["tie_out"]["failed_after"] >= 1
    assert res["template_checks"]["failed"] >= 1


def test_spend_cap_breach_stops_the_run(harness, monkeypatch):
    monkeypatch.setenv("TEMPO_MAPPER", "digest")
    import app.population.pipeline.plan as plan_mod

    def cap(*a, **k):
        raise SpendCapExceeded("cap hit")
    monkeypatch.setattr(plan_mod, "map_metrics", cap)
    with pytest.raises(SpendCapExceeded):
        _run()
    assert harness["uploads"] == []           # nothing delivered on a cap abort


def test_guarded_stage_failure_is_recorded_not_fatal(harness, monkeypatch):
    monkeypatch.setenv("TEMPO_MAPPER", "digest")
    import app.population.conservation as conservation
    monkeypatch.setattr(conservation, "family_gaps",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("graph exploded")))
    res = _run()
    assert len(res["filled"]) == 4            # the fill still delivered
    warnings = res["routing"].get("stage_warnings") or []
    assert any("conservation" in w for w in warnings)
