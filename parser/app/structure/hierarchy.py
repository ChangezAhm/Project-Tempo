"""Parent/child hierarchy for metric rows (the migration-plan linchpin).

The prototype captured indent_level but never linked rows. Here we link each
row to the nearest preceding row with a SMALLER indent level — the classic
P&L shape: Revenue(0) → Product Revenue(1) / Service Revenue(1) → Total(0).
Mutates each MetricRow's ``parent_row`` (the parent's row number) in place.
Operates per sheet, in row order.
"""

from __future__ import annotations

from app.structure.schema import MetricRow


def assign_hierarchy(rows: list[MetricRow]) -> list[MetricRow]:
    by_sheet: dict[str, list[MetricRow]] = {}
    for r in rows:
        by_sheet.setdefault(r.sheet_name, []).append(r)

    for sheet_rows in by_sheet.values():
        stack: list[MetricRow] = []  # ancestors, increasing indent
        for r in sorted(sheet_rows, key=lambda x: x.row):
            # Pop ancestors at the same or deeper indent — they're siblings/cousins.
            while stack and stack[-1].indent_level >= r.indent_level:
                stack.pop()
            r.parent_row = stack[-1].row if stack else None
            stack.append(r)
    return rows
