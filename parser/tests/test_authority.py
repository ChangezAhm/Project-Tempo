"""Phase 3+4 authority-model tests: lexicon categories are overridable PRIORS,
facts beat labels, and contract decisions carry scope.
"""

from app.datamodel.derive import _classify_category
from app.datamodel.merge import apply_corrections
from app.population.contract import decision_spec, load_decisions, source_fingerprint


# --- facts beat label priors ------------------------------------------------

def test_connector_formula_beats_junk_label():
    # A junk-looking label used to silently delete real connector data.
    cat, kind = _classify_category("Attributes", '=CX_GET("co","rev","2025-01","M")',
                                   None, "input", "Flash", set())
    assert cat == "sourced" and kind == "junk_label_connector"
    # without the connector the lexicon prior still applies
    cat, kind = _classify_category("Attributes", "", None, "input", "Flash", set())
    assert cat == "config" and kind == "scaffolding"


# --- merge: LLM judgment may override a lexicon PRIOR, never a fact ---------

def _fact(**kw):
    base = {"sheet_name": "S", "metric_label": "KPI Label 3", "category": "config",
            "category_source": "lexicon:placeholder", "scenario": "unknown",
            "basis": "unknown"}
    base.update(kw)
    return base


def _corr(created_by, patch, cid="c1"):
    return {"id": cid, "created_by": created_by,
            "match": {"metric_label": "KPI Label 3"}, "patch": patch}


def test_llm_overrides_lexicon_category_but_not_factual():
    # lexicon-sourced config -> LLM may reverse it to data (provenance stamped)
    facts, applied, unmatched = apply_corrections(
        [_fact()], [_corr("llm-enrichment", {"category": "data"})])
    assert facts[0]["category"] == "data" and facts[0]["category_source"] == "llm"
    assert applied == {"c1"} and not unmatched
    # a FACTUAL category (no lexicon source) is untouchable by the LLM
    facts, _, _ = apply_corrections(
        [_fact(category="computed", category_source=None)],
        [_corr("llm-enrichment", {"category": "data"})])
    assert facts[0]["category"] == "computed"


def test_user_overrides_anything():
    facts, _, _ = apply_corrections(
        [_fact(category="computed", category_source=None)],
        [_corr("user", {"category": "data"})])
    assert facts[0]["category"] == "data" and facts[0]["category_source"] == "user"


# --- contract scope ---------------------------------------------------------

def _snap(names):
    return {"sheets": [{"name": n} for n in names]}


def test_source_fingerprint_is_a_family_identity():
    jan = _snap(["P&L", "BS", "SaaS metrics"])
    feb = _snap(["SaaS metrics", "P&L", "BS"])          # same pack, new month
    other = _snap(["Sheet1", "Dump"])
    assert source_fingerprint(jan) == source_fingerprint(feb)
    assert source_fingerprint(jan) != source_fingerprint(other)


def test_source_scoped_questions_get_per_family_item_keys():
    # A SCALE_CONFLICT question is per SOURCE FAMILY: run.py files it with the
    # fingerprint in the item source, so answering source A's unit question can
    # never content-address-suppress the same question for source B.
    from app.review.items import make_item
    fp_a = source_fingerprint(_snap(["P&L", "BS"]))
    fp_b = source_fingerprint(_snap(["Export", "Dump"]))
    q = "'arr' — units for 'arr' could not be determined. How should this fill?"
    item_a = make_item(source=f"populate-plan:{fp_a[:8]}", kind="judgment", question=q)
    item_b = make_item(source=f"populate-plan:{fp_b[:8]}", kind="judgment", question=q)
    assert item_a["item_key"] != item_b["item_key"]


def test_source_format_decisions_apply_only_to_their_family(monkeypatch):
    fp = source_fingerprint(_snap(["P&L"]))
    rows = [
        {"status": "answered",
         "check_spec": decision_spec("arr", "source_unit", None,
                                     scope="source_format", fingerprint=fp),
         "resolution": {"answer": "USD '000"}},
        {"status": "answered",
         "check_spec": decision_spec("arr", "rollup", None),   # template scope
         "resolution": {"answer": "quarter-end"}},
    ]
    monkeypatch.setattr("app.supabase_client.list_review_items", lambda vid: rows)
    same = load_decisions("v1", fingerprint=fp)
    assert same == {"arr": {"source_unit": "USD '000", "rollup": "end"}}
    other = load_decisions("v1", fingerprint="ffffffffffffffff")
    assert other == {"arr": {"rollup": "end"}}   # the unit answer stays home
