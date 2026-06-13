from app.raw_extraction.schema import CellInfo, CellType, WorkbookMetadata
from app.raw_extraction.workbook_parser import ParsedSheet, ParsedWorkbook
from app.snapshot import SNAPSHOT_SCHEMA_VERSION, workbook_to_snapshot


def _workbook():
    wb = ParsedWorkbook()
    wb.metadata = WorkbookMetadata(filename="x.xlsx", sheet_count=1, total_cells=2)
    s = ParsedSheet(name="PL", index=0, is_hidden=False)
    s.used_max_row, s.used_max_col = 2, 2
    s.cells = [
        CellInfo(address="A1", row=1, col=1, value="Revenue", cell_type=CellType.STRING),
        CellInfo(
            address="B1", row=1, col=2, value="=A1", formula="=A1",
            cell_type=CellType.FORMULA, precedents=["PL!A1"],
        ),
    ]
    wb.sheets = [s]
    return wb


def test_snapshot_shape_and_content():
    snap = workbook_to_snapshot(_workbook())
    assert snap["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert snap["metadata"]["filename"] == "x.xlsx"
    assert len(snap["sheets"]) == 1
    sheet = snap["sheets"][0]
    assert sheet["name"] == "PL"
    assert len(sheet["cells"]) == 2
    # precedent ranges survive serialisation
    formula_cell = next(c for c in sheet["cells"] if c["formula"])
    assert formula_cell["precedents"] == ["PL!A1"]


def test_snapshot_is_json_serialisable():
    import json

    snap = workbook_to_snapshot(_workbook())
    # must round-trip cleanly (this is what gets gzipped to storage)
    assert json.loads(json.dumps(snap))["sheets"][0]["name"] == "PL"
