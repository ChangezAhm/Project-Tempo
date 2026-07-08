from app.raw_extraction.schema import CellInfo, CellType
from app.structure.field_extractor import extract_metric_rows


def _n(addr, row, col, value):
    return CellInfo(address=addr, row=row, col=col, value=value, cell_type=CellType.NUMBER)


def test_data_columns_beyond_100_are_scanned():
    # 20+ years of monthly periods exceed column CV (100); the scan follows the
    # sheet's used width (capped at 260) instead of stopping at a hard 100.
    cells = [
        CellInfo(address="A5", row=5, col=1, value="Revenue", cell_type=CellType.STRING),
        _n("E5", 5, 5, 1.0),
        _n("EE5", 5, 135, 2.0),
    ]
    rows = extract_metric_rows(cells, "S")
    assert rows and rows[0].data_cols == [5, 135]
    assert rows[0].data_range == "E5:EE5"
