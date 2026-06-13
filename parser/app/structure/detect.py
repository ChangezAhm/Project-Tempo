"""Layer-2 orchestrator: ParsedWorkbook → StructureResult (deterministic).

Order mirrors the prototype's mapping_builder but lean: per visible sheet,
extract metric rows → assign hierarchy → detect periods → collect section
signals; then detect input fields across the workbook (needs the formula
graph + the per-sheet periods). Hidden sheets are skipped (downstream filter).
"""

from __future__ import annotations

from datetime import date

from app.raw_extraction.workbook_parser import ParsedWorkbook
from app.structure.field_extractor import extract_metric_rows
from app.structure.hierarchy import assign_hierarchy
from app.structure.input_detector import build_input_fields
from app.structure.schema import DetectedPeriod, StructureResult
from app.structure.section_signals import find_section_title_signals, regions_to_signals
from app.structure.temporal_analyzer import detect_periods


def detect_structure(parsed: ParsedWorkbook, today: date | None = None) -> StructureResult:
    result = StructureResult()
    periods_by_sheet: dict[str, list[DetectedPeriod]] = {}

    for sheet in parsed.sheets:
        if sheet.is_hidden:
            continue

        rows = extract_metric_rows(sheet.cells, sheet.name)
        assign_hierarchy(rows)
        result.metric_rows.extend(rows)

        periods = detect_periods(sheet.cells, sheet.name, today)
        periods_by_sheet[sheet.name] = periods
        result.periods.extend(periods)

        result.regions.extend(regions_to_signals(sheet.regions))
        result.section_signals.extend(find_section_title_signals(sheet.cells, sheet.name))

    result.input_fields = build_input_fields(parsed, result.metric_rows, periods_by_sheet, today)
    return result
