"""Detect input fields — cells a user fills in.

Ported from template-compiler-prototype/src/parsing/input_detector.py.
Six-signal stack preserved; canonical-metric lookup removed (deferred to the
LLM). Periods are computed once by the orchestrator and passed in, so we don't
re-run detection per sheet here.

Signal precedence (any firing → input; signal 1 is authoritative):
  1. unlocked cell on a protected sheet (author-designated)
  2. in the formula graph's input set (referenced by formulas, not a formula)
  3. has downstream dependents
  4. input-style fill colour
  5. non-formula numeric/date cell in a period column
  6. mixed row (some formula cols, some not)
"""

from __future__ import annotations

from datetime import date

from app.raw_extraction.cell_analyzer import is_input_fill
from app.raw_extraction.schema import CellInfo, CellType
from app.raw_extraction.workbook_parser import ParsedWorkbook
from app.structure.schema import DetectedPeriod, InputField, MetricRow, PeriodStatus


def _bfs_downstream(start, dependents, max_depth=3, max_total=20):
    visited = {start}
    queue: list[tuple[str, int]] = [(start, 0)]
    result: list[str] = []
    while queue:
        cell, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for dep in dependents.get(cell, []):
            if dep in visited:
                continue
            visited.add(dep)
            result.append(dep)
            if len(result) >= max_total:
                return result
            queue.append((dep, depth + 1))
    return result


def build_input_fields(
    parsed: ParsedWorkbook,
    metric_rows: list[MetricRow],
    periods_by_sheet: dict[str, list[DetectedPeriod]],
    today: date | None = None,
) -> list[InputField]:
    if today is None:
        today = date.today()

    graph_input_addrs = parsed.formula_graph.input_cells

    # dependents: which formula cells reference each cell (link.source → target).
    dependents: dict[str, list[str]] = {}
    for link in parsed.formula_graph.links:
        dependents.setdefault(link.source, []).append(link.target)

    sheet_protected = {s.name: s.is_protected for s in parsed.sheets}
    out: list[InputField] = []

    for sheet in parsed.sheets:
        if sheet.is_hidden:
            continue

        periods = periods_by_sheet.get(sheet.name, [])
        period_by_col: dict[int, DetectedPeriod] = {p.col: p for p in periods}

        current_period_col = None
        current_period_label = ""
        for p in periods:
            if p.status == PeriodStatus.CURRENT:
                current_period_col = p.col
                current_period_label = p.label
                break

        sheet_cells: dict[tuple[int, int], CellInfo] = {(c.row, c.col): c for c in sheet.cells}
        is_protected = sheet_protected.get(sheet.name, False)
        sheet_rows = [r for r in metric_rows if r.sheet_name == sheet.name]

        for mr in sheet_rows:
            input_cols: list[int] = []
            formula_cols: list[int] = []
            input_evidence: list[str] = []
            row_is_unlocked = False

            for col in mr.data_cols:
                cell = sheet_cells.get((mr.row, col))
                if cell is None:
                    continue
                qualified = f"{sheet.name}!{cell.address}"

                if cell.cell_type == CellType.FORMULA:
                    formula_cols.append(col)
                    continue

                is_input = False
                reasons: list[str] = []
                if is_protected and not cell.style.is_locked:
                    is_input = True
                    row_is_unlocked = True
                    reasons.append("UNLOCKED cell on protected sheet (author-designated input)")
                if qualified in graph_input_addrs:
                    is_input = True
                    reasons.append("referenced by formulas (formula graph)")
                deps = dependents.get(qualified, [])
                if deps:
                    is_input = True
                    reasons.append(f"feeds into {len(deps)} formula(s)")
                if is_input_fill(cell.style.fill_color):
                    is_input = True
                    reasons.append(f"input-style fill color ({cell.style.fill_color})")
                if col in period_by_col and cell.cell_type in (CellType.NUMBER, CellType.DATE):
                    is_input = True
                    reasons.append(f"non-formula value in period column ({period_by_col[col].label})")

                if is_input:
                    input_cols.append(col)
                    input_evidence.extend(reasons)

            if formula_cols and input_cols:
                input_evidence.append(
                    f"mixed row: {len(input_cols)} input cols, {len(formula_cols)} formula cols"
                )

            if not input_cols:
                continue

            has_historical = any(
                period_by_col.get(col, DetectedPeriod(col=0, label="")).status == PeriodStatus.HISTORICAL
                for col in input_cols
            )

            needs_collection = False
            if current_period_col and current_period_col in input_cols:
                cell = sheet_cells.get((mr.row, current_period_col))
                if cell is None or cell.value is None or cell.value == 0 or cell.value == "":
                    needs_collection = True

            row_dependents: list[str] = []
            row_downstream: list[str] = []
            seen_downstream: set[str] = set()
            for col in input_cols:
                cell = sheet_cells.get((mr.row, col))
                if cell:
                    qualified = f"{sheet.name}!{cell.address}"
                    row_dependents.extend(dependents.get(qualified, [])[:5])
                    for d in _bfs_downstream(qualified, dependents, max_depth=3, max_total=10):
                        if d not in seen_downstream:
                            seen_downstream.add(d)
                            row_downstream.append(d)
                            if len(row_downstream) >= 20:
                                break
                if len(row_downstream) >= 20:
                    break

            out.append(InputField(
                sheet_name=sheet.name,
                row=mr.row,
                label_text=mr.label_text,
                label_cell=mr.label_cell,
                input_columns=input_cols,
                formula_columns=formula_cols,
                current_period_col=current_period_col,
                current_period_label=current_period_label,
                is_unlocked=row_is_unlocked,
                needs_collection=needs_collection,
                has_historical_data=has_historical,
                unit=mr.unit,
                number_format=mr.number_format,
                sample_value=mr.sample_value,
                named_range=mr.named_range,
                indent_level=mr.indent_level,
                dependent_formulas=list(set(row_dependents))[:10],
                downstream_cells=row_downstream,
                input_evidence=input_evidence,
            ))

    return out
