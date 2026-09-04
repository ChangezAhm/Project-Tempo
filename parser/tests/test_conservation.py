"""Conservation guards: partially-consumed source families surface their orphans
(the missing-share-based-payments class), and additions exclude series that
duplicate already-consumed information."""

from types import SimpleNamespace

from app.population.conservation import family_gaps, redundant_series
from app.population.schema import MetricMap


def _cell(row, col, value=None, formula=None, precedents=None):
    return {"row": row, "col": col, "address": f"c{row}_{col}",
            "value": value, "formula": formula, "precedents": precedents or []}


def _snapshot():
    """r28 = SUM(r24:r27) — the add-backs family. r14 = r12 - r13 (gross profit,
    derived). r20 = SUM(r16:r19) — an opex total whose children are leaves."""
    cells = [
        _cell(24, 3, 10.0), _cell(25, 3, 5.0), _cell(26, 3, 3.0), _cell(27, 3, 2.0),
        _cell(28, 3, formula="=SUM(C24:C27)", precedents=["C24:C27"]),
        _cell(12, 3, 100.0), _cell(13, 3, -40.0),
        _cell(14, 3, formula="=C12+C13", precedents=["C12", "C13"]),
        _cell(15, 3, formula="=C14/C12", precedents=["C14", "C12"]),
        _cell(16, 3, 1.0), _cell(17, 3, 2.0), _cell(18, 3, 3.0), _cell(19, 3, 4.0),
        _cell(20, 3, formula="=SUM(C16:C19)", precedents=["C16:C19"]),
    ]
    return {"sheets": [{"name": "MA", "cells": cells}]}


def _catalogue(rows):
    return {f"MA!r{r}": SimpleNamespace(id=f"MA!r{r}", sheet="MA", row=r,
                                        label=f"row {r}") for r in rows}


def test_family_gap_finds_the_orphan_sibling():
    cat = _catalogue([24, 25, 26, 27, 28])
    maps = [MetricMap(metric="One off costs", series_id="MA!r24"),
            MetricMap(metric="Exceptionals", series_id="MA!r25"),
            MetricMap(metric="CMO salary", series_id="MA!r26")]
    gaps = family_gaps(_snapshot(), cat, maps)
    assert len(gaps) == 1
    g = gaps[0]
    assert g["total_row"] == 28 and g["total_used"] is False
    assert [o["sid"] for o in g["orphans"]] == ["MA!r27"]     # the SBP line
    assert {c["metric"] for c in g["consumed"]} == {"One off costs", "Exceptionals", "CMO salary"}


def test_consumed_total_still_surfaces_component_orphans():
    # the template filled its TOTAL line from r28; the component lines are
    # display rows too — orphan components must surface (flagged total_used)
    cat = _catalogue([24, 25, 26, 27, 28])
    maps = [MetricMap(metric="Total EBITDA adjustments", series_id="MA!r28"),
            MetricMap(metric="Exceptionals", series_id="MA!r25")]
    gaps = family_gaps(_snapshot(), cat, maps)
    assert len(gaps) == 1
    g = gaps[0]
    assert g["total_used"] is True and g["total_metric"] == "Total EBITDA adjustments"
    assert {o["sid"] for o in g["orphans"]} == {"MA!r24", "MA!r26", "MA!r27"}


def test_families_use_direct_children_not_pierced_leaves():
    # investing total r62 = r49(total capex, USED) + r50: its DIRECT children are
    # r49/r50 — r47/r48 (inside the used r49) must NOT surface as its orphans
    # (that pierced expansion caused a real 2x Capex double-count).
    snap = _snapshot()
    snap["sheets"][0]["cells"] += [
        {"row": 47, "col": 3, "address": "c47", "value": -41.8, "formula": None, "precedents": []},
        {"row": 48, "col": 3, "address": "c48", "value": -200.6, "formula": None, "precedents": []},
        {"row": 49, "col": 3, "address": "c49", "value": None,
         "formula": "=SUM(C47:C48)", "precedents": ["C47:C48"]},
        {"row": 50, "col": 3, "address": "c50", "value": -10.0, "formula": None, "precedents": []},
        {"row": 62, "col": 3, "address": "c62", "value": None,
         "formula": "=C49+C50", "precedents": ["C49", "C50"]},
    ]
    cat = _catalogue([47, 48, 49, 50, 62])
    maps = [MetricMap(metric="Capex", series_id="MA!r49"),
            MetricMap(metric="Exceptional CF", series_id="MA!r50")]
    gaps = {g["total_row"]: g for g in family_gaps(snap, cat, maps)}
    # r62's DIRECT children are r49/r50 (both used) — r47/48 never leak through it
    assert 62 not in gaps
    # they surface only under their OWN family (r49), flagged total_used — the
    # placement containment guard is what forbids summing them back into Capex
    assert gaps[49]["total_used"] is True
    assert {o["sid"] for o in gaps[49]["orphans"]} == {"MA!r47", "MA!r48"}


def test_containment_guard_refuses_summing_a_component_into_its_total(monkeypatch):
    import app.llm as llm
    from app.population.conservation import place_orphans

    snap = _snapshot()
    snap["sheets"][0]["cells"] += [
        {"row": 47, "col": 3, "address": "c47", "value": -41.8, "formula": None, "precedents": []},
        {"row": 48, "col": 3, "address": "c48", "value": -200.6, "formula": None, "precedents": []},
        {"row": 49, "col": 3, "address": "c49", "value": None,
         "formula": "=SUM(C47:C48)", "precedents": ["C47:C48"]},
    ]
    maps = [MetricMap(metric="Capex", series_id="MA!r49", confidence=0.9)]
    gaps = [{"sheet": "MA", "total_row": 49, "total_label": "Total capex",
             "total_used": True, "total_metric": "Capex", "consumed": [],
             "orphans": [{"sid": "MA!r47", "label": "Maintenance capex"}]}]
    fake = ('{"placements":[{"series_id":"MA!r47","action":"add_to",'
            '"metric":"Capex","reason":"capex component"}]}')
    monkeypatch.setattr(llm, "guarded_stream", lambda **kw: (None, fake))
    placed, excluded, leftovers = place_orphans(
        gaps, maps, [{"metric": "Capex"}], snap)
    assert placed == 0                       # guard refused the double-count
    assert maps[0].also_series_ids == []
    assert [o["sid"] for o in leftovers] == ["MA!r47"]   # goes to review instead


def test_redundant_series_derived_and_children_of_used_totals():
    cat = _catalogue([12, 13, 14, 15, 16, 17, 18, 19, 20])
    used = {"MA!r12", "MA!r13", "MA!r20"}
    red = redundant_series(_snapshot(), cat, used)
    assert "MA!r14" in red        # gross profit = used r12 + used r13
    assert "MA!r15" in red        # margin over a derived row (fixpoint)
    assert "MA!r16" in red        # child of the consumed opex total
    assert "MA!r19" in red
    assert "MA!r12" not in red    # used rows are not 'redundant'


def test_redundant_empty_without_formulas_or_usage():
    cat = _catalogue([12, 13])
    assert redundant_series({"sheets": [{"name": "MA", "cells": [_cell(12, 3, 1.0)]}]},
                            cat, {"MA!r12"}) == set()
    assert redundant_series(_snapshot(), cat, set()) == set()


def test_derivable_kpis_survive_display_label_gate():
    # 'Gross profit' duplicates a template display line -> redundant;
    # 'LTM net revenue' is derivable from the same rows but is a NOVEL display
    # line (the KPI itself) -> kept. Children of used totals stay excluded.
    from app.population.conservation import _label_tokens
    snap = _snapshot()
    cat = {
        "MA!r12": SimpleNamespace(id="MA!r12", sheet="MA", row=12, label="Revenue"),
        "MA!r13": SimpleNamespace(id="MA!r13", sheet="MA", row=13, label="Cost of sales"),
        "MA!r14": SimpleNamespace(id="MA!r14", sheet="MA", row=14, label="Gross profit"),
        "MA!r15": SimpleNamespace(id="MA!r15", sheet="MA", row=15, label="LTM net revenue"),
        "MA!r16": SimpleNamespace(id="MA!r16", sheet="MA", row=16, label="Staff costs"),
        "MA!r20": SimpleNamespace(id="MA!r20", sheet="MA", row=20, label="Total opex"),
    }
    used = {"MA!r12", "MA!r13", "MA!r20"}
    display = {_label_tokens("Revenue"), _label_tokens("Cost of sales"),
               _label_tokens("Total opex"), _label_tokens("Gross profit")}
    red = redundant_series(snap, cat, used, display_labels=display)
    assert "MA!r14" in red        # display dupe of a template line
    assert "MA!r15" not in red    # derivable, but a novel KPI line
    assert "MA!r16" in red        # child of the consumed opex total
