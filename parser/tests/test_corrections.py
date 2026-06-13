from app.datamodel.merge import apply_corrections


def _fact(**kw):
    base = {"sheet_name": "BS", "cell": "AD20", "canonical_metric": "fixed_assets",
            "scenario": "actual", "basis": "unknown", "category": "data",
            "scenario_source": "deterministic", "basis_source": "default",
            "applied_correction_ids": []}
    base.update(kw)
    return base


def test_metric_level_correction_applies_to_all_matching():
    facts = [
        _fact(cell="AD20"), _fact(cell="AE20"),               # fixed_assets
        _fact(cell="AD24", canonical_metric="inventory"),     # different metric
    ]
    corrections = [{"id": "c1", "match": {"canonical_metric": "fixed_assets"},
                    "patch": {"basis": "point_in_time"}, "note": "BS lines are point-in-time"}]
    facts, applied, unmatched = apply_corrections(facts, corrections)
    fa = [f for f in facts if f["canonical_metric"] == "fixed_assets"]
    assert all(f["basis"] == "point_in_time" for f in fa)
    assert all(f["basis_source"] == "user" for f in fa)               # provenance marked
    assert all("c1" in f["applied_correction_ids"] for f in fa)
    assert facts[2]["basis"] == "unknown"                              # inventory untouched
    assert applied == {"c1"} and unmatched == []


def test_empty_match_applies_to_everything():
    facts = [_fact(), _fact(canonical_metric="inventory")]
    facts, applied, _ = apply_corrections(facts, [{"id": "g", "match": {}, "patch": {"category": "data"}}])
    assert "g" in applied


def test_unmatched_correction_surfaced():
    facts = [_fact()]
    corr = [{"id": "x", "match": {"canonical_metric": "does_not_exist"}, "patch": {"basis": "ytd"}, "note": "stale"}]
    facts, applied, unmatched = apply_corrections(facts, corr)
    assert applied == set()
    assert len(unmatched) == 1 and unmatched[0]["id"] == "x"
    assert facts[0]["basis"] == "unknown"                              # nothing changed


def test_scenario_patch_marks_source_and_category():
    facts = [_fact(scenario="actual")]
    corr = [{"id": "s", "match": {"sheet_name": "BS"}, "patch": {"scenario": "forecast", "category": "config"}}]
    facts, _, _ = apply_corrections(facts, corr)
    assert facts[0]["scenario"] == "forecast" and facts[0]["scenario_source"] == "user"
    assert facts[0]["category"] == "config"
