"""Template-side preparation: the workbook to fill, its snapshot-derived
context (formats/magnitudes/timeline dates), its own check cells, the business
context channel, per-metric slot facts — and the perception layer (both
workbooks as address-tagged grids + sheet images) for grid mode."""

from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from app import supabase_client as sb
from app.population.catalogue import effective_value
from app.population.context import load_context
from app.population.periods import parse_any_date, sheet_grains
from app.population.pipeline.state import RunState
from app.population.schema import metric_key

logger = logging.getLogger(__name__)


def _download_to_temp(storage_path: str, filename: str) -> Path:
    data = sb.download_workbook(storage_path)
    fd, name = tempfile.mkstemp(suffix=Path(filename).suffix or ".xlsx")
    os.close(fd)
    p = Path(name)
    p.write_bytes(data)
    return p


def _bytes_to_temp(filename: str, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(suffix=Path(filename).suffix or ".xlsx")
    os.close(fd)
    p = Path(name)
    p.write_bytes(data)
    return p


def _pick_timeline(row_dates: dict[int, dict[int, object]]) -> dict[int, object]:
    """The row that is the sheet's period header: prefer rows whose dates increase
    left→right (a real timeline). A DATA row whose values happen to fall in the
    Excel date-serial range (29k–60k, e.g. thousands-scale amounts) parses as
    'dates' too, but financial series aren't monotonic by column — this stops such
    a row from hijacking the timeline and garbling date alignment."""
    def monotonic(d: dict[int, object]) -> bool:
        vals = [d[c] for c in sorted(d)]
        return all(a <= b for a, b in zip(vals, vals[1:]))

    mono = [d for d in row_dates.values() if len(d) >= 3 and monotonic(d)]
    pool = mono or list(row_dates.values())
    return max(pool, key=len)


def _load_template_snap(version_id: str) -> dict | None:
    """The template's stored snapshot, or None. Best-effort — populate degrades
    (no scale/date context, no check discovery) rather than failing."""
    try:
        return json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    except Exception as e:  # noqa: BLE001
        logger.info("template snapshot unavailable (%s)", e)
        return None


def _template_context(snap: dict | None) -> tuple[dict, dict, dict]:
    """From the template snapshot, the maps verify/execute need:
      - numfmt[(sheet, A1)]      -> number format (kind/currency for scale)
      - mags[(sheet, row)]       -> numeric magnitudes already in the row (scale by
                                    what the cell holds, not by its unit label)
      - dates_by_col[(sheet,col)]-> the column's real period date, read from the
                                    sheet's timeline header row, so periods align
                                    by actual date.
    Best-effort — empty on any failure (execute then uses label scale + positional)."""
    numfmt: dict[tuple[str, str], str] = {}
    mags: dict[tuple[str, int], list[float]] = defaultdict(list)
    dates_by_col: dict[tuple[str, int], object] = {}
    if not snap:
        return {}, {}, {}
    for s in snap.get("sheets", []):
        name = s.get("name")
        row_dates: dict[int, dict[int, object]] = defaultdict(dict)   # row -> {col: date}
        for c in s.get("cells", []):
            addr = (c.get("address") or "").upper()
            nf = (c.get("style") or {}).get("number_format")
            if addr and nf:
                numfmt[(name, addr)] = nf
            ev = effective_value(c)
            if isinstance(ev, (int, float)) and not isinstance(ev, bool) and ev:
                mags[(name, c.get("row"))].append(float(ev))
            d = parse_any_date(ev)
            if d is not None:
                row_dates[c["row"]][c["col"]] = d
        if row_dates:
            for col, d in _pick_timeline(row_dates).items():
                dates_by_col[(name, col)] = d
    return numfmt, dict(mags), dates_by_col


def _attach_slot_facts(metrics: list[dict], facts: list[dict],
                       template_context: tuple[dict, dict, dict]) -> None:
    """Attach per-metric SLOT FACTS for the planner: which sheets demand this
    metric, at what grain, over what date range, and whether the rows already
    hold numbers (scale evidence). Facts only — the planner judges from them."""
    _numfmt, mags_by_row, dates_by_col = template_context or ({}, {}, {})
    sheet_dates: dict[str, list] = {}
    for (sh, _c), d in dates_by_col.items():
        sheet_dates.setdefault(sh, []).append(d)
    grains = sheet_grains(dates_by_col)
    per_metric: dict[str, dict[str, int]] = {}
    has_values: dict[str, bool] = {}
    for f in facts:
        k = metric_key(f)
        if not k:
            continue
        sh = f.get("sheet_name")
        per_metric.setdefault(k, {})[sh] = per_metric.get(k, {}).get(sh, 0) + 1
        if mags_by_row.get((sh, f.get("row"))):
            has_values[k] = True
    for m in metrics:
        sheets = per_metric.get(m.get("metric"), {})
        if not sheets:
            continue
        parts = []
        for sh, n in sheets.items():
            ds = sorted(sheet_dates.get(sh, []))
            if ds:
                g = grains.get(sh) or "?"
                parts.append(f"{sh}: {g}ly {ds[0]:%Y-%m}..{ds[-1]:%Y-%m} ({n} slots)")
            else:
                parts.append(f"{sh}: undated ({n} slots)")
        summary = "; ".join(parts)
        if not has_values.get(m.get("metric")):
            summary += " — rows currently empty"
        m["slots"] = summary


def _reset_preview(version_id: str, facts: list[dict]) -> dict:
    """What a reset would touch, BEFORE any spend: in-scope input cells split by
    what they hold (stale numeric literal vs connector/formula with a cached
    value). Best-effort — empty on any failure."""
    try:
        snap = json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    except Exception:  # noqa: BLE001
        return {}
    cell_info: dict[tuple[str, str], tuple[bool, object]] = {}
    for s in snap.get("sheets", []):
        for c in s.get("cells", []):
            addr = (c.get("address") or "").upper()
            if addr:
                v = c.get("value")
                is_formula = bool(c.get("formula")) or (isinstance(v, str) and v.startswith("="))
                cell_info[(s["name"], addr)] = (is_formula, effective_value(c))
    stale_values = formula_cells = 0
    for f in facts:
        info = cell_info.get((f.get("sheet_name"), (f.get("cell") or "").upper()))
        if info is None:
            continue
        is_formula, ev = info
        if is_formula:
            if ev not in (None, ""):
                formula_cells += 1
        elif isinstance(ev, (int, float)) and not isinstance(ev, bool):
            stale_values += 1
    return {"cells_in_scope": len(facts),
            "stale_values_cleared_by_standard_reset": stale_values,
            "stale_connector_cells_cleared_only_by_full_reset": formula_cells}


def _build_grid_block(t_snap: dict | None, target_inputs: list[dict],
                      source_snapshot: dict) -> tuple[str | None, list[str], list[str]]:
    """(grids_block, notes, demand_sheets) — both workbooks as address-tagged
    grids. Shared by the live run and the dry-run cost estimate, so the price
    the user consents to is computed from the same bytes the run will send."""
    from app.population.grids import workbook_grids
    from app.population.source_understanding import select_sheets
    demand_sheets = sorted({f.get("sheet_name") for f in target_inputs if f.get("sheet_name")})
    tpl_part, tnotes = (workbook_grids(
        t_snap, demand_sheets, title="TEMPLATE WORKBOOK (the file being filled)")
        if t_snap else ("", ["template snapshot unavailable — source grids only"]))
    src_names = [s.get("name") for s in select_sheets(source_snapshot)]
    src_part, snotes = workbook_grids(
        source_snapshot, src_names, title="SOURCE WORKBOOK (the data to map FROM)")
    return f"{tpl_part}\n\n{src_part}".strip(), (tnotes + snotes)[:10], demand_sheets


# ---- stages ------------------------------------------------------------------

def stage_load_template(state: RunState) -> None:
    """Template workbook/snapshot/context load FIRST — grid mode reads BOTH
    workbooks before any source pass runs (the one-pass call needs them)."""
    state.t_vid, t_path, t_fn = sb.get_latest_file(state.target_template_id)
    try:
        state.tgt_tmp = _download_to_temp(t_path, t_fn)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("Template workbook is missing from storage — re-upload this template.") from e

    # Template cell formats + per-row magnitudes — lets scale be decided by what the
    # template cell actually holds (robust) instead of by unit labels (a mess).
    state.t_snap = _load_template_snap(state.t_vid)
    state.template_context = _template_context(state.t_snap)

    # The template's OWN check cells (Checks sheets, tie-outs, OK/ERROR flags) —
    # discovered up front so render_filled can recalculate and read them back.
    if state.t_snap:
        try:
            from app.population.template_checks import collect_check_cells
            from app.understanding.persist import get_understanding
            try:
                t_und = get_understanding(state.target_template_id)
            except Exception:  # noqa: BLE001 — checks degrade to name/shape discovery
                t_und = None
            state.template_check_cells = collect_check_cells(state.t_snap, t_und)
        except Exception as e:  # noqa: BLE001 — verification must never block a fill
            logger.warning("check discovery failed: %s", e)

    # The business-context channel: sponsor notes + answered review questions +
    # strict author rules ride into every mapping batch. Best-effort — an empty
    # context degrades to label/definition matching, never blocks the run.
    state.biz_context = load_context(state.target_template_id, state.t_vid)

    # SLOT FACTS ride into every mapping variant (grain/date-range/emptiness
    # per metric) — attach before any model sees the demand.
    _attach_slot_facts(state.demand["metrics"], state.target_inputs, state.template_context)


def stage_grids(state: RunState) -> None:
    """GRID MODE (default): grids + sheet images for the one-pass / grid mapper.
    The model reads the source structure AND maps with both workbooks in
    context. Output stays claims + MetricMaps — reconcile, catalogue, verify,
    execute and every guard run unchanged (eyes, not hands). Any failure falls
    back to the two-pass digest path, loudly. TEMPO_MAPPER=digest restores the
    legacy pipeline wholesale."""
    if os.environ.get("TEMPO_MAPPER", "grid").lower() != "grid":
        return
    try:
        from app.population.source_understanding import select_sheets
        state.grids_block, state.grid_notes, demand_sheets = _build_grid_block(
            state.t_snap, state.target_inputs, state.source_snapshot)
        src_sheets_sel = [s.get("name") for s in select_sheets(state.source_snapshot)]
        # SHEET IMAGES — layout aid only, grids stay authoritative. One tile
        # per sheet; template file is always on disk, source file on the
        # upload path only (the add-in snapshot has no file to render).
        try:
            from app.understanding.sheet_image import render_sheet_tiles
            for path, names in ((state.tgt_tmp, demand_sheets[:3]),
                                (state.source_path, src_sheets_sel[:3])):
                if path is None:
                    continue
                for nm in names:
                    try:
                        tiles = render_sheet_tiles(path, nm, max_tiles=1)
                        if tiles:
                            cap, png = tiles[0]
                            state.grid_images.append((f"[layout image] {cap or nm}", png))
                    except Exception:  # noqa: BLE001 — a failed render never blocks
                        pass
        except Exception as e:  # noqa: BLE001
            logger.info("sheet images unavailable (%s) — grids only", e)
    except Exception as e:  # noqa: BLE001
        logger.warning("grid build failed (%s) — digest path", e)
        state.grids_block = None
