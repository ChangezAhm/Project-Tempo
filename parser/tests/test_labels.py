from app.labels import derive_column_headers, derive_row_labels
from app.raw_extraction.schema import CellInfo, CellType


def _cell(addr, row, col, value, ctype=CellType.STRING):
    return CellInfo(address=addr, row=row, col=col, value=value, cell_type=ctype)


def test_row_labels_first_string_in_left_columns():
    cells = [
        _cell("A1", 1, 1, "Revenue"),
        _cell("C1", 1, 3, 100, CellType.NUMBER),
        _cell("B2", 2, 2, "COGS"),
        _cell("A2", 2, 1, "", CellType.STRING),  # empty string ignored → B2 wins
    ]
    labels = derive_row_labels(cells)
    assert labels["1"] == "Revenue"
    assert labels["2"] == "COGS"


def test_row_labels_ignore_far_columns_and_numbers():
    cells = [
        _cell("H1", 1, 8, "N"),                     # too far right (col > 3)
        _cell("A3", 3, 1, 42, CellType.NUMBER),     # not a string
    ]
    assert derive_row_labels(cells) == {}


def test_column_headers_topmost_string_per_column():
    cells = [
        _cell("A1", 1, 1, "Metric"),
        _cell("C1", 1, 3, "Jan-26"),
        _cell("C5", 5, 3, "later"),                 # lower than C1 → ignored
        _cell("D20", 20, 4, "below band"),          # row > 12 header band → ignored
    ]
    headers = derive_column_headers(cells)
    assert headers["A"] == "Metric"
    assert headers["C"] == "Jan-26"
    assert "D" not in headers
