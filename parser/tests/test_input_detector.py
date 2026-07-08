from app.raw_extraction.formula_mapper import build_formula_graph_from_precedents
from app.raw_extraction.schema import CellInfo, CellStyle, CellType
from app.raw_extraction.workbook_parser import ParsedSheet, ParsedWorkbook
from app.structure.input_detector import build_input_fields
from app.structure.schema import DetectedPeriod, MetricRow, PeriodStatus


def _workbook(protected: bool):
    wb = ParsedWorkbook()
    s = ParsedSheet(name="S", index=0, is_hidden=False)
    s.is_protected = protected
    s.cells = [
        CellInfo(address="A5", row=5, col=1, value="Revenue", cell_type=CellType.STRING),
        CellInfo(address="E5", row=5, col=5, value=100, cell_type=CellType.NUMBER,
                 style=CellStyle(is_locked=False)),  # explicitly unlocked
        CellInfo(address="F5", row=5, col=6, value="=E5*2", formula="=E5*2",
                 cell_type=CellType.FORMULA, precedents=["S!E5"]),
    ]
    wb.sheets = [s]
    wb.formula_graph = build_formula_graph_from_precedents(
        precedents_by_cell={"S!F5": ["S!E5"]},
        formula_addrs={"S!F5"},
        populated_cells={"S!A5", "S!E5", "S!F5"},
        formula_strings={"S!F5": "=E5*2"},
    )
    return wb


_ROW = MetricRow(
    sheet_name="S", row=5, label_text="Revenue", label_cell="A5",
    label_col=1, data_cols=[5, 6],
)


def test_unlocked_cell_on_protected_sheet_is_input():
    fields = build_input_fields(_workbook(protected=True), [_ROW], {})
    assert len(fields) == 1
    f = fields[0]
    assert 5 in f.input_columns          # E5 detected as input
    assert 6 in f.formula_columns        # F5 is a formula
    assert f.is_unlocked is True
    assert any("UNLOCKED" in e for e in f.input_evidence)


def test_unprotected_falls_back_to_graph_signal():
    fields = build_input_fields(_workbook(protected=False), [_ROW], {})
    assert len(fields) == 1
    f = fields[0]
    assert 5 in f.input_columns          # still detected: in formula-graph input set + feeds F5
    assert f.is_unlocked is False        # locking is meaningless on an unprotected sheet
    assert any("formula graph" in e or "feeds into" in e for e in f.input_evidence)


def _current_period_workbook(value, cell_type):
    wb = ParsedWorkbook()
    s = ParsedSheet(name="S", index=0, is_hidden=False)
    s.is_protected = True
    s.cells = [
        CellInfo(address="A5", row=5, col=1, value="Revenue", cell_type=CellType.STRING),
        CellInfo(address="E5", row=5, col=5, value=value, cell_type=cell_type,
                 style=CellStyle(is_locked=False)),  # author-designated input
    ]
    wb.sheets = [s]
    wb.formula_graph = build_formula_graph_from_precedents(
        precedents_by_cell={}, formula_addrs=set(),
        populated_cells={"S!A5", "S!E5"}, formula_strings={},
    )
    return wb


_PERIODS = {"S": [DetectedPeriod(col=5, label="Mar-26", status=PeriodStatus.CURRENT)]}
_ZERO_ROW = MetricRow(sheet_name="S", row=5, label_text="Revenue", label_cell="A5",
                      label_col=1, data_cols=[5])


def test_entered_zero_is_not_needs_collection():
    # A literal 0 in the current-period cell is an entered value, not a gap
    # (L3 rule, understanding/prompts.py) — only None/"" mean empty.
    wb = _current_period_workbook(0, CellType.NUMBER)
    fields = build_input_fields(wb, [_ZERO_ROW], _PERIODS)
    assert fields and fields[0].needs_collection is False


def test_empty_current_period_cell_needs_collection():
    wb = _current_period_workbook(None, CellType.EMPTY)
    fields = build_input_fields(wb, [_ZERO_ROW], _PERIODS)
    assert fields and fields[0].needs_collection is True
