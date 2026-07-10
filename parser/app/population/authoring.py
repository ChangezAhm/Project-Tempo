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
    for each. Three tiers, most trustworthy first:
      1. DATE match — region column and series column in the same calendar MONTH
         bucket (31-Jan matches 1-Jan, pick_column's rule).
      2. POSITIONAL — when NO region column carries a date at all, align
         newest-anchored (rightmost region column ↔ rightmost dated-or-not source
         column), each value tagged match='positional' so the run flags it for
         review instead of silently guessing.
    A region with SOME dated columns never falls back positionally — a partial
    timeline means the undated columns are something else (labels, totals)."""
    dated = [(col, d) for col, d in value_col_dates if col is not None and d is not None]
    if dated:
        out: list[dict] = []
        for col, d in dated:
            for scol, sd, _pt in series.period_cols:
                if sd is not None and _bucket(d, "month") == _bucket(sd, "month"):
                    out.append({"col": col,
                                "source_sheet": series.sheet,
                                "source_cell": f"{_col_letters(scol)}{series.row}",
                                "match": "date"})
                    break    # first matching source column wins (period_cols order is stable)
        return out

    # fully undated region → positional, newest-anchored, flagged
    rcols = sorted(col for col, _d in value_col_dates if col is not None)
    scols = sorted(c for (c, _d, _pt) in series.period_cols)
    if not rcols or not scols:
        return []
    out = []
    for i in range(1, min(len(rcols), len(scols)) + 1):
        out.append({"col": rcols[-i],
                    "source_sheet": series.sheet,
                    "source_cell": f"{_col_letters(scols[-i])}{series.row}",
                    "match": "positional"})
    out.reverse()
    return out


def _region_slots(region: dict) -> list[dict]:
    """The region's slots, oldest model rows synthesized as all-blank. Ordered by
    write preference: blank first (non-destructive), then placeholder, then
    editable_label (both approval-gated)."""
    slots = region.get("slots") or []
    if not slots:
        total_row = region.get("total_row")
        slots = [{"row": r, "mode": "blank", "current_label": None}
                 for r in range(region["row_start"], region["row_end"] + 1)
                 if r != total_row]
    order = {"blank": 0, "placeholder": 1, "editable_label": 2}
    return sorted((s for s in slots if s.get("mode") in order),
                  key=lambda s: (order[s["mode"]], s["row"]))


def propose_additions(catalogue: dict[str, "Series"], used_series_ids: set[str],
                      regions: list[dict], *, max_per_region: int | None = None,
                      region_candidates: dict[int, list[str]] | None = None
                      ) -> tuple[list[dict], list[str]]:
    """Propose NEW template lines for source series that mapped to no template
    metric. Deterministic — no LLM, no I/O; returns (proposals, notes).

    regions: extensible-region dicts (migration 0008 shape): sheet_name, kind,
    label_col, value_cols=[{col, parsed_date}], row_start, row_end, total_row,
    rules, confidence.

    Rules enforced here:
      - only UNUSED series are candidates (a mapped series already has a home);
      - a candidate fits a region only if >=1 value_col matches one of its period
        columns (date-bucket, or flagged positional when the region is undated —
        see _matched_values);
      - SLOTS are assigned blank-first (non-destructive), then placeholder, then
        editable_label; occupied-slot proposals carry slot_mode + expected_label
        and are approval-gated at apply time;
      - the total row is NEVER proposed into;
      - a series is placed at most ONCE across all regions;
      - ``region_candidates`` (region index -> ordered series ids) lets the
        bridge inject a per-region ranking (e.g. adjustment-lexicon series for an
        adjustment_rows region); regions without an entry use the default pool.

    Each proposal also carries ``total_row``/``row_start`` from its region —
    apply_additions needs them for the totals-safety re-check and for the
    sibling-row style copy.
    """
    # Sort candidates by (sheet, row) so proposals are stable run-to-run —
    # a review artifact that churns between identical runs destroys trust.
    default_candidates = sorted((s for sid, s in catalogue.items() if sid not in used_series_ids),
                                key=lambda s: (s.sheet, s.row, s.id))

    proposals: list[dict] = []
    notes: list[str] = []
    placed: set[str] = set()

    for idx, region in enumerate(regions):
        total_row = region.get("total_row")
        slots = _region_slots(region)
        cap = len(slots) if max_per_region is None else min(len(slots), max_per_region)

        ranked_ids = (region_candidates or {}).get(idx)
        if ranked_ids is not None:
            candidates = [catalogue[sid] for sid in ranked_ids
                          if sid in catalogue and sid not in used_series_ids]
        else:
            candidates = default_candidates

        # Parse the region's column dates once (they arrive as ISO-ish strings).
        value_col_dates = [(vc.get("col"), parse_any_date(vc.get("parsed_date")))
                           for vc in (region.get("value_cols") or [])]

        taken = 0
        overflow = 0
        positional_used = False
        for s in candidates:
            if s.id in placed:
                continue
            values = _matched_values(s, value_col_dates)
            if not values:
                continue    # no period overlap -> this series doesn't belong here
            if taken >= cap:
                overflow += 1   # fits, but the region is full — report, don't force
                continue
            slot = slots[taken]
            if any(v.get("match") == "positional" for v in values):
                positional_used = True
            proposals.append({
                "sheet_name": region["sheet_name"],
                "row": slot["row"],
                "label_col": region["label_col"],
                "label": s.label,
                "kind": region.get("kind"),
                "source_series_id": s.id,
                "unit": _compact_unit(s.unit),
                "values": values,
                "region_rules": region.get("rules"),
                "confidence": region.get("confidence"),
                # slot write policy (apply enforces it):
                "slot_mode": slot.get("mode") or "blank",
                "expected_label": slot.get("current_label"),
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
        if positional_used:
            notes.append(
                f"region {region['sheet_name']}!r{region['row_start']}-r{region['row_end']}: "
                "columns carry no dates — values aligned POSITIONALLY (newest-anchored); verify alignment")

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
      - BLANK slots: the label cell must still be EMPTY in the live workbook
        (the region map could be stale) — occupied -> skip;
      - PLACEHOLDER / EDITABLE_LABEL slots: the proposal must be APPROVED
        (p['approved'] is True — a human said yes via the review inbox), and the
        live label text must EQUAL expected_label exactly (drift since detection
        -> skip); the overwrite is recorded in the applied record;
      - a live FORMULA cell (label or value) is never written;
      - source values get the same guards as apply.py: None/'' and
        formula/error strings ('=...', '#REF!') are never written; only values
        that coerce to a number are;
      - style copy is wrapped so a style failure can't lose a written value.
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

        label_addr = f"{_col_letters(p['label_col'])}{row}"
        label_cell = ws.cells.get(label_addr)
        if getattr(label_cell, "is_formula", False):
            _skip(f"label cell {label_addr} holds a formula — never overwritten")
            continue

        mode = (p.get("slot_mode") or "blank").lower()
        overwrote = None
        if mode == "blank":
            # Re-verify emptiness in the LIVE workbook: the region map was computed
            # from a snapshot and could be stale — overwriting an occupied label
            # would destroy someone's structure.
            if label_cell.value not in (None, ""):
                _skip(f"label cell {label_addr} is not empty")
                continue
        else:   # placeholder / editable_label — destructive, so approval-gated
            if p.get("approved") is not True:
                _skip(f"{mode} slot needs approval before its label can be replaced")
                continue
            live = str(label_cell.value).strip() if label_cell.value not in (None, "") else ""
            expected = str(p.get("expected_label") or "").strip()
            if live != expected:
                _skip(f"label at {label_addr} changed since detection "
                      f"({live[:24]!r} != {expected[:24]!r}) — not overwritten")
                continue
            overwrote = {"from": live, "to": p.get("label")}

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
            if getattr(cell, "is_formula", False):
                continue    # never write over a live formula cell
            cell.put_value(num)
            _copy_style(ws, style_row, v["col"], cell)
            written += 1

        record = {"sheet_name": p["sheet_name"], "row": row,
                  "label": p.get("label"), "cells_written": written,
                  "source_series_id": p.get("source_series_id")}
        if overwrote:
            record["overwrote_label"] = overwrote   # audited, never silent
        applied.append(record)

    return applied, skipped
