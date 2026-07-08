"""Add-line-item authoring (Population-Authoring-Plan, Part 2C).

Binding fills the template's FIXED input cells; anything the source reports that
maps to no template metric is currently just dropped. Here those leftover series
become PROPOSED new lines inside the template's extensible regions (the areas the
template invites additions — detected upstream, stored with the contract):

  propose_additions  — deterministic matcher: unmapped series -> (region, row,
                       per-period source cells). No LLM, no I/O; proposals are a
                       review artifact (new lines are higher-risk than value
                       fills, so a human approves them before anything is written).
  apply_additions    — writes approved proposals into an open workbook. Writes
                       into the next free blank rows only — NEVER inserts rows
                       (inserting into a ~90%-formula model risks breaking
                       references) and NEVER touches the total row (its SUM
                       already spans the blank range).
"""

from __future__ import annotations

from datetime import date

from app.population.catalogue import Series
from app.population.periods import _bucket, parse_any_date
from app.population.units import Unit


def _col_letters(col: int) -> str:
    """1-based column index -> Excel letters (1->A, 27->AA). (Same as binding's.)"""
    s = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return s


def _compact_unit(unit: Unit | None) -> str | None:
    """The series' unit as a short reviewer-facing string ('money USD',
    'percent', ...). We deliberately do NOT filter proposals by unit kind — a
    percent series proposed into a money-shaped region is for the REVIEWER to
    reject, and the unit string is what lets them see it."""
    if unit is None or unit.kind == "unknown":
        return None
    if unit.kind == "money":
        return f"money {unit.currency}" if unit.currency else "money"
    return unit.kind    # percent / ratio


def _matched_values(series: Series, value_col_dates: list[tuple[int, date | None]],
                    ) -> list[dict]:
    """The region value-columns this series can feed, with the exact source cell
    for each. A region column matches a series period column when both carry a
    real date in the same calendar MONTH bucket (31-Jan matches 1-Jan — same rule
    pick_column applies). A value_col with no parsed date matches NOTHING: with
    no date we'd be guessing alignment, and a proposed line must never guess."""
    out: list[dict] = []
    for col, d in value_col_dates:
        if d is None or col is None:
            continue    # conservative: dateless region columns are not fillable
        for scol, sd, _pt in series.period_cols:
            if sd is not None and _bucket(d, "month") == _bucket(sd, "month"):
                out.append({"col": col,
                            "source_sheet": series.sheet,
                            "source_cell": f"{_col_letters(scol)}{series.row}"})
                break    # first matching source column wins (period_cols order is stable)
    return out


def propose_additions(catalogue: dict[str, "Series"], used_series_ids: set[str],
                      regions: list[dict], *, max_per_region: int | None = None
                      ) -> tuple[list[dict], list[str]]:
    """Propose NEW template lines for source series that mapped to no template
    metric. Deterministic — no LLM, no I/O; returns (proposals, notes).

    regions: extensible-region dicts (migration 0008 shape): sheet_name, kind,
    label_col, value_cols=[{col, parsed_date}], row_start, row_end, total_row,
    rules, confidence.

    Rules enforced here:
      - only UNUSED series are candidates (a mapped series already has a home);
      - a candidate fits a region only if >=1 value_col date-bucket-matches one
        of its period columns (see _matched_values);
      - rows are assigned top-down, one proposal per row, within capacity
        (row_start..row_end minus the total row) and ``max_per_region``;
        overflow is reported in notes, never forced;
      - the total row is NEVER proposed into;
      - a series is placed at most ONCE across all regions (two proposals for
        one series would double-write the same data).

    Each proposal also carries ``total_row``/``row_start`` from its region —
    apply_additions needs them for the totals-safety re-check and for the
    sibling-row style copy.
    """
    # Sort candidates by (sheet, row) so proposals are stable run-to-run —
    # a review artifact that churns between identical runs destroys trust.
    candidates = sorted((s for sid, s in catalogue.items() if sid not in used_series_ids),
                        key=lambda s: (s.sheet, s.row, s.id))

    proposals: list[dict] = []
    notes: list[str] = []
    placed: set[str] = set()

    for region in regions:
        total_row = region.get("total_row")
        # Free rows = the region's row range minus the total row. One proposal
        # per row; we never insert rows, so capacity is hard.
        rows = [r for r in range(region["row_start"], region["row_end"] + 1)
                if r != total_row]
        cap = len(rows) if max_per_region is None else min(len(rows), max_per_region)

        # Parse the region's column dates once (they arrive as ISO-ish strings).
        value_col_dates = [(vc.get("col"), parse_any_date(vc.get("parsed_date")))
                           for vc in (region.get("value_cols") or [])]

        taken = 0
        overflow = 0
        for s in candidates:
            if s.id in placed:
                continue
            values = _matched_values(s, value_col_dates)
            if not values:
                continue    # no date overlap -> this series doesn't belong here
            if taken >= cap:
                overflow += 1   # fits, but the region is full — report, don't force
                continue
            row = rows[taken]
            proposals.append({
                "sheet_name": region["sheet_name"],
                "row": row,
                "label_col": region["label_col"],
                "label": s.label,
                "kind": region.get("kind"),
                "source_series_id": s.id,
                "unit": _compact_unit(s.unit),
                "values": values,
                "region_rules": region.get("rules"),
                "confidence": region.get("confidence"),
                # apply-side safety context (not reviewer-facing):
                "total_row": total_row,
                "row_start": region["row_start"],
            })
            placed.add(s.id)
            taken += 1
        if overflow:
            notes.append(
                f"region {region['sheet_name']}!r{region['row_start']}-r{region['row_end']} "
                f"full: {overflow} candidates skipped")

    return proposals, notes


def _copy_style(ws, src_row: int, col: int, target_cell) -> None:
    """Copy the sibling cell's style onto a written cell so the new line looks
    native. Strictly best-effort: a style failure must NEVER lose the value that
    was just written, so everything here is swallowed."""
    if src_row < 1:
        return
    try:
        style = ws.cells.get(f"{_col_letters(col)}{src_row}").get_style()
        target_cell.set_style(style)
    except Exception:   # noqa: BLE001 — cosmetics only, by design
        pass


def apply_additions(ws_by_name: dict, proposals: list[dict], sval: dict
                    ) -> tuple[list[dict], list[dict]]:
    """Write approved proposals into an OPEN workbook. Returns
    (applied_records, skipped_records); every proposal ends up in exactly one.

    ws_by_name: {sheet_name: worksheet} as built in run.render_filled.
    sval: {(source_sheet, A1): value} source value map, as apply.apply_links
    builds it — values are read from HERE, never re-typed.

    Worksheet surface (duck-typed on purpose, so tests can use a fake):
      ws.cells.get(a1: str) -> cell
      cell.value                       # current value; None/'' == empty
      cell.put_value(v)                # write a value
      cell.get_style() / cell.set_style(style)   # best-effort formatting copy

    Safety, in order:
      - the total row is never written (re-checked here even though
        propose_additions already excluded it);
      - the label cell must still be EMPTY in the live workbook (the region map
        could be stale, or a fixed label could sit there) — occupied -> skip;
      - source values get the same guards as apply.py: None/'' and
        formula/error strings ('=...', '#REF!') are never written; only values
        that coerce to a number are (a new line's values are numeric series —
        stray text is noise, not data);
      - style copy (from the row above row_start, the last native-formatted
        sibling) is wrapped so a style failure can't lose a written value.
    """
    applied: list[dict] = []
    skipped: list[dict] = []

    for p in proposals:
        def _skip(reason: str) -> None:
            skipped.append({"sheet_name": p.get("sheet_name"), "row": p.get("row"),
                            "label": p.get("label"),
                            "source_series_id": p.get("source_series_id"),
                            "reason": reason})

        ws = ws_by_name.get(p.get("sheet_name"))
        if ws is None:
            _skip("sheet not found in workbook")
            continue
        row = p["row"]
        # NEVER the total row — it already sums the range (assert-skip).
        if p.get("total_row") is not None and row == p.get("total_row"):
            _skip("row is the region's total row")
            continue

        # Re-verify the label cell is empty in the LIVE workbook: the region map
        # was computed from a snapshot and could be stale — overwriting an
        # occupied label would destroy someone's structure.
        label_addr = f"{_col_letters(p['label_col'])}{row}"
        label_cell = ws.cells.get(label_addr)
        if label_cell.value not in (None, ""):
            _skip(f"label cell {label_addr} is not empty")
            continue

        style_row = (p.get("row_start") or row) - 1   # last native sibling above the region
        label_cell.put_value(p.get("label"))
        _copy_style(ws, style_row, p["label_col"], label_cell)

        written = 0
        for v in p.get("values") or []:
            raw = sval.get((v.get("source_sheet"), (v.get("source_cell") or "").upper()))
            # Same guards as apply.py: an empty cell or a formula/error string
            # is not a value — never write it into the template.
            if raw in (None, ""):
                continue
            if isinstance(raw, str) and raw.strip()[:1] in ("=", "#"):
                continue
            if isinstance(raw, bool):
                continue
            try:
                num = float(raw)
            except (TypeError, ValueError):
                continue    # non-numeric text in a value column is noise
            cell = ws.cells.get(f"{_col_letters(v['col'])}{row}")
            cell.put_value(num)
            _copy_style(ws, style_row, v["col"], cell)
            written += 1

        applied.append({"sheet_name": p["sheet_name"], "row": row,
                        "label": p.get("label"), "cells_written": written,
                        "source_series_id": p.get("source_series_id")})

    return applied, skipped
