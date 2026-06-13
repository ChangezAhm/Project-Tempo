import json

from app.understanding.per_sheet import to_strict_schema
from app.understanding.schema import SheetUnderstanding


def _check_object(node):
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False
            assert set(node["required"]) == set(node["properties"].keys())
        for v in node.values():
            _check_object(v)
    elif isinstance(node, list):
        for item in node:
            _check_object(item)


def test_every_object_is_strict():
    schema = to_strict_schema(SheetUnderstanding)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    assert schema.get("$defs"), "nested models should produce $defs"
    _check_object(schema)


def test_metric_row_has_interpretation_fields():
    from app.understanding.schema import InterpretationSource, MetricRow
    for f in ("definition", "qualification_criteria", "expected_source", "interpretation_source"):
        assert f in MetricRow.model_fields
    assert {e.value for e in InterpretationSource} == {"template_stated", "model_knowledge", "inferred"}


def test_input_cell_format_accepts_cells_and_ranges():
    from app.understanding.per_sheet import _ADDR_RE
    assert _ADDR_RE.match("D41")
    assert _ADDR_RE.match("T44:V49")      # ranges are valid input specs
    assert _ADDR_RE.match("AO72")
    assert not _ADDR_RE.match("Sheet!A1")  # qualified refs are not bare addresses
    assert not _ADDR_RE.match("notacell")


def test_no_unsupported_keywords():
    # NB: "title" is a legitimate FIELD name on Section, so it appears as a
    # property key — we only forbid unsupported constraint keywords + "default".
    blob = json.dumps(to_strict_schema(SheetUnderstanding))
    for kw in ('"minimum"', '"maximum"', '"maxLength"', '"minItems"', '"pattern"', '"default"'):
        assert kw not in blob, f"unsupported keyword leaked: {kw}"
