"""One-pass understand+map: the model reads the source structure AND maps in a
single call, referencing series by LABEL CELL; deterministic code turns cells
into catalogue ids (orientation-aware) and translates the plan. Plus the
outcome-driven revision pass."""

import app.population.mapping as mapping
from app.population.catalogue import catalogue_from_understanding
from tests.test_transposed import _claims, _snapshot


def test_translate_sources_resolves_cells_to_ids_incl_transposed():
    sid_index: dict = {}
    cat = catalogue_from_understanding(_snapshot(), _claims(), sid_index=sid_index)
    raw = [
        {"metric": "Revenue", "status": "direct", "source": "PL!G4",
         "confidence": 0.9},
        {"metric": "Total costs", "status": "aggregate", "source": "PL!H4",
         "also_sources": ["PL!I4", "PL!ZZ99"], "confidence": 0.8},
        {"metric": "Ghost", "status": "direct", "source": "PL!Q40", "confidence": 0.7},
    ]
    maps, notes = mapping.translate_sources(raw, sid_index, set(cat))
    by = {m.metric: m for m in maps}
    assert by["Revenue"].series_id == "PL!c7"            # transposed: column id
    assert by["Total costs"].series_id == "PL!c8"
    assert by["Total costs"].also_series_ids == ["PL!c9"]   # ZZ99 dropped, noted
    assert any("ZZ99" in n for n in notes)
    assert by["Ghost"].series_id is None                 # unresolvable primary
    assert by["Ghost"].status == "unavailable" and "did not resolve" in by["Ghost"].note


def test_understand_and_map_single_call_and_parse(monkeypatch):
    calls = []
    reply = ('{"sheets":[{"sheet":"PL","periods":[{"header_cell":"B5",'
             '"date":"2024-01-28","grain":"month","kind":"actual"}],'
             '"series":[{"label_cell":"G4","label":"Revenue"}]}],'
             '"mappings":[{"metric":"Revenue","status":"direct","source":"PL!G4",'
             '"confidence":0.9}]}')

    def fake_stream(**kw):
        calls.append(kw)
        return None, reply

    monkeypatch.setattr(mapping, "guarded_stream", fake_stream)
    sheets, raw, degraded = mapping.understand_and_map(
        [{"metric": "Revenue", "label": "Revenue"}], "### GRIDS ###",
        images=[("PL layout", b"\x89PNG fake")])
    assert len(calls) == 1
    assert calls[0]["model"] == mapping.MODEL_SMART
    assert calls[0]["n_images"] == 1
    blocks = calls[0]["content"]
    assert isinstance(blocks, list) and blocks[-1]["type"] == "text"
    assert "### GRIDS ###" in blocks[-2]["text"]      # grids ride the CACHED stable block
    assert calls[0]["cache_blocks"] == len(blocks) - 1
    assert "TEMPLATE METRICS" in blocks[-1]["text"]   # per-call part stays uncached
    assert degraded is False
    assert sheets[0]["sheet"] == "PL" and raw[0]["source"] == "PL!G4"


def test_understand_and_map_raises_on_unusable_reply(monkeypatch):
    monkeypatch.setattr(mapping, "guarded_stream",
                        lambda **kw: (None, "sorry, no JSON here"))
    try:
        mapping.understand_and_map([{"metric": "X"}], "grids")
        raise AssertionError("should have raised")
    except ValueError:
        pass   # caller falls back to two-pass — loudly


def test_revise_plan_returns_only_revised_entries(monkeypatch):
    reply = ('{"mappings":[{"metric":"Capex","status":"direct","series_id":"PL!c9",'
             '"confidence":0.85,"note":"the grid shows total capex on column I"}]}')
    monkeypatch.setattr(mapping, "guarded_stream", lambda **kw: (None, reply))
    cat = catalogue_from_understanding(_snapshot(), _claims())
    out = mapping.revise_plan(
        [{"metric": "Capex", "plan": {"status": "unavailable"},
          "outcome": "filled 0 cells; blanks: 40x no source series"}],
        cat, "### GRIDS ###")
    assert len(out) == 1 and out[0].series_id == "PL!c9"
    assert mapping.revise_plan([], cat, "grids") == []   # nothing to do, no call


def test_onepass_truncation_sets_degraded_and_retries_without_thinking(monkeypatch):
    calls = []
    reply = ('{"sheets":[{"sheet":"PL","periods":[{"header_cell":"B5"}],'
             '"series":[{"label_cell":"G4","label":"Revenue"}]}],'
             '"mappings":[{"metric":"Revenue","status":"direct","source":"PL!G4"}]}')

    def fake_stream(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("LLM call truncated at max_tokens=32000 (site=grid_onepass)")
        return None, reply

    monkeypatch.setattr(mapping, "guarded_stream", fake_stream)
    sheets, raw, degraded = mapping.understand_and_map([{"metric": "Revenue"}], "grids")
    assert degraded is True
    assert calls[1]["thinking"] is False              # retry frees the budget
    assert "temperature" not in calls[1]              # Opus 4.8 rejects the param
    assert sheets and raw


def test_revise_for_checks_formats_failures_and_parses(monkeypatch):
    calls = []
    reply = ('{"mappings":[{"metric":"Revenue","status":"direct","series_id":"PL!c7",'
             '"confidence":0.9,"note":"commissions were double-counted into revenue"}]}')

    def fake(**kw):
        calls.append(kw)
        return None, reply

    monkeypatch.setattr(mapping, "guarded_stream", fake)
    cat = catalogue_from_understanding(_snapshot(), _claims())
    from app.population.schema import MetricMap
    maps = [MetricMap(metric="Revenue", series_id="PL!c8", confidence=0.8)]
    fails = [{"sheet": "Checks", "cell": "D7", "label": "EBITDA ties to statutory P&L",
              "after": -483.2}]
    out = mapping.revise_for_checks(fails, maps, cat, "### GRIDS ###",
                                    flags=["Non_Underlying: not catalogued"])
    assert out and out[0].series_id == "PL!c7"
    var = calls[0]["content"][1]["text"]
    assert "Checks!D7" in var and "EBITDA ties" in var       # the exact failure
    assert "Revenue -> PL!c8" in var                          # the current plan
    assert "Non_Underlying" in var                            # known gaps ride along
    assert "Never force a fill just to make a check pass" in var
    assert calls[0]["cache_blocks"] == 1                      # grids prefix cached
    assert mapping.revise_for_checks([], maps, cat, "g") == []
