"""Population entry points (Build A): parse-source is reused upload+parse, then
the staged pipeline runs understand (LLM) → map (LLM) → verify/execute
(deterministic) → render filled workbook + attribution.

The orchestration itself lives in ``app.population.pipeline`` (one stage per
concern over an explicit RunState — see docs/Refactor-Population-Pipeline.md);
this module keeps the public API stable: ``populate_from_bytes``,
``populate_from_snapshot``, ``build_demand``, ``render_filled``.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.population import source_cache
from app.population.pipeline import run_pipeline
from app.population.pipeline.state import RunState
# Re-exports — the stable import surface (tests/benchmarks/main.py use these):
from app.population.pipeline.demand import _meaningless_key, build_demand  # noqa: F401
from app.population.pipeline.deliver import _is_clearable_value, render_filled  # noqa: F401
from app.population.pipeline.prepare import (_bytes_to_temp, _pick_timeline,  # noqa: F401
                                             _template_context)
from app.raw_extraction.workbook_parser import parse_workbook
from app.snapshot import workbook_to_snapshot
from app.structure.detect import detect_structure


def _run_population(target_template_id: str, source_snapshot: dict,
                    source_periods: dict[str, list[dict]], source_label: str,
                    as_of_date: str | None, *, content_hash: str | None = None,
                    source_path: Path | None = None,
                    display_unit: str | None = None, reset: str = "values",
                    add_lines: str = "apply", dry_run: bool = False,
                    deep_rescue: bool = True, link_sources: bool = True) -> dict:
    """Core: understand the SOURCE with AI (period columns + data series + units,
    cached by file), build the catalogue from that, ask the LLM to map template
    metrics → source series, then verify+execute the plan (periods/scale-by-
    magnitude/sign) and read the real (cached) values from the snapshot. The
    template is NOT re-read; we only need its workbook to write the values into.
    Everything is under the spend cap."""
    return run_pipeline(RunState(
        target_template_id=target_template_id, source_snapshot=source_snapshot,
        source_periods=source_periods, source_label=source_label,
        as_of_date=as_of_date, content_hash=content_hash, source_path=source_path,
        display_unit=display_unit, reset=reset, add_lines=add_lines,
        dry_run=dry_run, deep_rescue=deep_rescue, link_sources=link_sources))


def _detect_source_periods(parsed) -> dict[str, list[dict]]:
    """Per-sheet period columns from deterministic detection — real dates the
    executor aligns template slots against. {sheet: [{col, parsed_date, period_type}]}."""
    out: dict[str, list[dict]] = {}
    for p in detect_structure(parsed).periods:
        out.setdefault(p.sheet_name, []).append(
            {"col": p.col, "parsed_date": p.parsed_date, "period_type": p.period_type})
    return out


def populate_from_snapshot(target_template_id: str, source_filename: str, snapshot: dict,
                           as_of_date: str | None = None, *, display_unit: str | None = None,
                           reset: str = "values", add_lines: str = "apply",
                           dry_run: bool = False, deep_rescue: bool = True,
                           link_sources: bool = True) -> dict:
    """Populate from a CLIENT-SERIALIZED workbook snapshot — the Excel add-in
    path: the user's open workbook is read in place via Office.js (values,
    formulas, number formats) and posted as JSON; no file ever leaves Excel.
    Reconstructing a ParsedWorkbook from the snapshot reuses the exact same
    deterministic period detection as the upload path."""
    from app.reconstruct import reconstruct_workbook_from_snapshot

    parsed = reconstruct_workbook_from_snapshot(snapshot)
    source_periods = _detect_source_periods(parsed)
    ch = source_cache.content_hash(
        json.dumps(snapshot, sort_keys=True, default=str).encode())
    return _run_population(target_template_id, snapshot, source_periods,
                           source_filename or "workbook", as_of_date,
                           content_hash=ch, source_path=None,
                           display_unit=display_unit, reset=reset,
                           add_lines=add_lines, dry_run=dry_run, deep_rescue=deep_rescue,
                           link_sources=link_sources)


def populate_from_bytes(target_template_id: str, source_filename: str, source_bytes: bytes,
                        as_of_date: str | None = None, *, display_unit: str | None = None,
                        reset: str = "values", add_lines: str = "apply",
                        dry_run: bool = False, deep_rescue: bool = True,
                        link_sources: bool = True) -> dict:
    """Populate a template directly from an uploaded data file's bytes. Parses
    the source in-memory (Aspose → snapshot) — it is never stored as a template.
    This is the drag-a-file-onto-a-template path.

    Pass dry_run=True to get a cost estimate without any LLM call. display_unit
    lets the consultant declare the output basis (e.g. 'EUR millions') so scale
    resolves deterministically when the template carries no unit signals."""
    src_tmp = _bytes_to_temp(source_filename, source_bytes)
    try:
        parsed = parse_workbook(src_tmp)
        snapshot = workbook_to_snapshot(parsed)
        source_periods = _detect_source_periods(parsed)   # deterministic fallback only
        return _run_population(target_template_id, snapshot, source_periods,
                               source_filename or "source.xlsx", as_of_date,
                               content_hash=source_cache.content_hash(source_bytes),
                               source_path=src_tmp,   # alive until the run returns → images
                               display_unit=display_unit, reset=reset,
                               add_lines=add_lines, dry_run=dry_run, deep_rescue=deep_rescue,
                               link_sources=link_sources)
    finally:
        src_tmp.unlink(missing_ok=True)
