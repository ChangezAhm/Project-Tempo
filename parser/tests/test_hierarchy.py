from app.structure.hierarchy import assign_hierarchy
from app.structure.schema import MetricRow


def _r(row, label, indent):
    return MetricRow(
        sheet_name="PL", row=row, label_text=label,
        label_cell=f"A{row}", label_col=1, indent_level=indent,
    )


def test_parent_child_linkage():
    rows = [
        _r(5, "Revenue", 0),
        _r(6, "Product Revenue", 1),
        _r(7, "Service Revenue", 1),
        _r(8, "Total Revenue", 0),
        _r(9, "COGS", 0),
        _r(10, "Materials", 1),
    ]
    assign_hierarchy(rows)
    by_row = {r.row: r for r in rows}

    assert by_row[5].parent_row is None          # top-level
    assert by_row[6].parent_row == 5             # Product under Revenue
    assert by_row[7].parent_row == 5             # Service under Revenue
    assert by_row[8].parent_row is None          # Total back at top level
    assert by_row[9].parent_row is None
    assert by_row[10].parent_row == 9            # Materials under COGS


def test_deeper_nesting():
    rows = [_r(1, "A", 0), _r(2, "B", 1), _r(3, "C", 2), _r(4, "D", 1)]
    assign_hierarchy(rows)
    by_row = {r.row: r for r in rows}
    assert by_row[2].parent_row == 1
    assert by_row[3].parent_row == 2   # C(2) under B(1)
    assert by_row[4].parent_row == 1   # D(1) pops C and B, back under A


def test_hierarchy_is_per_sheet():
    a = MetricRow(sheet_name="A", row=2, label_text="x", label_cell="A2", label_col=1, indent_level=1)
    b = MetricRow(sheet_name="B", row=1, label_text="y", label_cell="A1", label_col=1, indent_level=0)
    assign_hierarchy([a, b])
    # a's parent must not be b (different sheet); no smaller-indent row on sheet A
    assert a.parent_row is None
