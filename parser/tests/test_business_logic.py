"""Offline proof that captured business logic reaches decisions (the 3 gaps):
1. mapping sees definitions/qualification criteria + the context block,
2. sign conventions are ENFORCED as post-fill checks,
3. user knowledge (notes/answers/rules) assembles into the context channel.
No API calls.
"""

from app.population.checks import sign_violations, violations_to_review_items
from app.population.context import build_context
from app.population.mapping import _metric_lines, _user_text
from app.population.schema import FilledCell


# --- Gap 1: mapping prompt carries the business logic -----------------------

def test_metric_lines_include_definition_and_qualification():
    m = [{"metric": "adj_ebitda", "label": "Adjusted EBITDA", "unit": "€'000",
          "definition": "EBITDA after normalising one-off items",
          "qualification_criteria": "Only Type I adjustments; excludes one-off legal costs"}]
    line = _metric_lines(m)
    assert "def: EBITDA after normalising" in line
    assert "qualifies: Only Type I adjustments" in line


def test_user_text_places_context_first_and_marks_authoritative():
    txt = _user_text([{"metric": "revenue", "label": "Revenue"}], "S1 | P&L | Sales",
                     context="SPONSOR NOTES:\n  ARR means contracted ARR only.")
    assert txt.index("TEMPLATE CONTEXT") < txt.index("SOURCE SERIES")
    assert "authoritative" in txt
    # no context -> no empty header
    assert "TEMPLATE CONTEXT" not in _user_text([{"metric": "revenue"}], "S1")


# --- Gap 2: conventions become checks ---------------------------------------

def _fill(cell, value, metric="cogs"):
    return FilledCell(template_sheet="PL", template_cell=cell, value=value,
                      raw_source_value=value, source_sheet="Src", source_cell="C5",
                      metric=metric, period_index=0, scenario="actual", confidence=0.9)


def _fact(cell, sign):
    return {"sheet_name": "PL", "cell": cell, "sign_convention": sign}


def test_sign_violation_detected():
    v = sign_violations([_fill("E9", 4812.5)],
                        [_fact("E9", "negative (entered as negative, summed in GP)")])
    assert len(v) == 1 and v[0]["expected"] == "negative" and v[0]["value"] == 4812.5


def test_sign_ok_and_edge_cases_pass():
    facts = [_fact("E9", "negative (costs)"), _fact("E10", "positive"), _fact("E11", None)]
    fills = [_fill("E9", -4812.5), _fill("E10", 120.0), _fill("E11", -5.0),
             _fill("E9", 0.0)]         # zero carries no sign signal
    assert sign_violations(fills, facts) == []


def test_violations_become_stable_review_items():
    v = sign_violations([_fill("E9", 4812.5)], [_fact("E9", "negative (costs)")])
    items = violations_to_review_items(v, "aurora.xlsx")
    assert items[0]["source"] == "populate" and items[0]["kind"] == "judgment"
    assert "PL!E9" in items[0]["question"]
    # key must NOT depend on the value, so a re-run re-binds to the same item
    v2 = sign_violations([_fill("E9", 9999.0)], [_fact("E9", "negative (costs)")])
    items2 = violations_to_review_items(v2, "other.xlsx")
    assert items[0]["item_key"] == items2[0]["item_key"]


# --- Gap 3: the context channel ----------------------------------------------

def test_build_context_prioritises_and_caps():
    ctx = build_context(
        "P&L in €'000; BS in €m. ARR = contracted ARR only.",
        [{"question": "Is Covenant EBITDA LTM?", "resolution": {"answer": "Yes, trailing 12 months."}},
         {"question": "unanswered", "resolution": {}}],   # no answer -> excluded
        [{"category": "sign_convention", "description": "Costs entered negative", "is_strict": True}],
    )
    assert ctx.index("SPONSOR NOTES") < ctx.index("CONFIRMED ANSWERS") < ctx.index("TEMPLATE RULES")
    assert "contracted ARR" in ctx and "trailing 12 months" in ctx and "Costs entered negative" in ctx
    assert "unanswered" not in ctx


def test_build_context_empty_sources_yield_empty_string():
    assert build_context(None, [], []) == ""
    assert build_context("   ", [], []) == ""


def test_build_context_hard_cap():
    ctx = build_context("x" * 10_000, [], [], max_chars=500)
    assert len(ctx) <= 500
