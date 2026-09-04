"""Traceable deliverable — the filled workbook re-expressed as FORMULAS.

The values deliverable answers WHAT was written; this one answers WHERE FROM
and HOW: every filled cell becomes a real Excel formula pointing at the user's
own source data, which is imported into the same workbook as values-only
"Source - …" sheets. The formula reproduces apply_links' arithmetic exactly —
aggregation (SUM/AVERAGE over the cited component cells), unit scale, sign
flip — so the population logic is inspectable in Excel itself, cell by cell.

Trust rules (AI owns meaning, code owns facts):
- Formulas are PRESENTATION of already-executed links; nothing is re-decided
  here. The operand addresses are the ones apply_links actually read.
- Imported source sheets are values-only. A live source formula could reference
  sheets that were renamed or not imported (or connector functions that go
  #NAME?) and silently break; cached values can't.
- Every written formula is recalculated IN PLACE (per-cell — a workbook-level
  calc would recompute the template's connector cells into #NAME?) and checked
  against the value the values deliverable wrote. A cell that disagrees falls
  back to the raw value and is counted — the two deliverables can never show
  different numbers.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable

from app.population.apply import _first_addr
from app.population.catalogue import effective_value
from app.population.schema import CellLink, FilledCell

logger = logging.getLogger(__name__)

# Excel sheet-name rules: <=31 chars, none of []:*?/\ , no leading/trailing
# apostrophe. The prefix marks imported sheets apart from the template's own.
SOURCE_SHEET_PREFIX = "Source - "
_ILLEGAL_SHEET_CHARS = set("[]:*?/\\")
_MAX_SHEET_NAME = 31


# --- pure planning / formula construction (no Aspose, unit-testable) ---------

def plan_source_sheet_names(needed: list[str], existing: Iterable[str]) -> dict[str, str]:
    """Excel-legal, collision-free names for the imported source sheets.
    ``existing`` = the template's own sheet names (a source sheet is often
    called the same thing — 'PL' vs 'PL' — so imports are ALWAYS renamed)."""
    taken = {(n or "").strip().lower() for n in existing}
    out: dict[str, str] = {}
    for orig in needed:
        base = "".join(ch for ch in orig if ch not in _ILLEGAL_SHEET_CHARS).strip().strip("'") or "Sheet"
        name = (SOURCE_SHEET_PREFIX + base)[:_MAX_SHEET_NAME].rstrip().rstrip("'")
        if name.lower() in taken:
            for i in range(2, 1000):
                suffix = f" ({i})"
                cand = (SOURCE_SHEET_PREFIX + base)[: _MAX_SHEET_NAME - len(suffix)].rstrip() + suffix
                if cand.lower() not in taken:
                    name = cand
                    break
        taken.add(name.lower())
        out[orig] = name
    return out


def _sheet_ref(name: str) -> str:
    """Always-quoted sheet reference — quoting is legal for every name and
    removes the 'when does Excel need quotes' question entirely."""
    return "'" + name.replace("'", "''") + "'"


def _num_lit(x: float) -> str:
    """A scale factor as an Excel-parseable literal ('0.001', '1e-06')."""
    return f"{x:.12g}"


def cited_sheets(link: CellLink) -> list[str]:
    """Every source sheet a link's operands live on (primary + any component
    cited with an explicit 'Sheet!' prefix), deduped, in citation order."""
    names = [link.source_sheet]
    for spec in link.agg_source_cells or []:
        s = (spec or "").strip()
        if "!" in s:
            names.append(s.split("!", 1)[0].strip())
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        k = n.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(n.strip())
    return out


def _operands(link: CellLink, resolve: Callable[[str], str | None]) -> list[str] | None:
    """Fully-qualified operand refs in apply_links' order, or None when any
    sheet can't be resolved to an imported one (the caller then keeps the raw
    value — a formula with a broken ref must never be written)."""
    prim = resolve(link.source_sheet)
    addr = _first_addr(link.source_cell)
    if not prim or not addr:
        return None
    refs = [f"{_sheet_ref(prim)}!{addr}"]
    for spec in link.agg_source_cells or []:
        s = (spec or "").strip()
        if "!" in s:
            sh_raw, a_raw = s.split("!", 1)
            sh = resolve(sh_raw.strip())
        else:
            sh, a_raw = prim, s
        a = _first_addr(a_raw)
        if not sh or not a:
            return None
        refs.append(f"{_sheet_ref(sh)}!{a}")
    return refs


def formula_for(link: CellLink, resolve: Callable[[str], str | None], *,
                is_text: bool = False) -> str | None:
    """The Excel formula that reproduces apply_links for this link:
    ``=-(SUM('Source - PL'!C12,'Source - PL'!D12))*0.001``. Text fills mirror
    apply's pass-through: a bare reference, no arithmetic (text never
    aggregates — apply reports such links unmatched before a fill exists)."""
    refs = _operands(link, resolve)
    if refs is None:
        return None
    if is_text:
        return "=" + refs[0]
    if len(refs) > 1:
        fn = "AVERAGE" if link.agg_op == "avg" else "SUM"
        core = f"{fn}({','.join(refs)})"
    else:
        core = refs[0]
    if link.unit_scale != 1.0:
        core = f"{core}*{_num_lit(link.unit_scale)}"
    if link.sign_flip:
        core = f"-({core})"
    return "=" + core


def _values_agree(computed, expected) -> bool:
    """The recalculated formula result vs the value the values deliverable
    wrote. Numeric: relative tolerance (float round-trips through Excel calc);
    otherwise exact-after-strip."""
    if (isinstance(computed, (int, float)) and not isinstance(computed, bool)
            and isinstance(expected, (int, float)) and not isinstance(expected, bool)):
        a, b = float(computed), float(expected)
        return abs(a - b) <= max(1e-9, 1e-6 * max(abs(a), abs(b)))
    return str(computed).strip() == str(expected).strip()


# --- Aspose: import source sheets + swap values for formulas -----------------

def _formulas_to_values(ws) -> None:
    """Freeze a copied source sheet to its cached values. remove_formulas keeps
    each cell's last value; the manual fallback does the same cell-by-cell."""
    try:
        ws.cells.remove_formulas()
        return
    except Exception:  # noqa: BLE001 — API surface varies; fall through
        pass
    stale = [(c.name, c.value) for c in ws.cells if c.is_formula]
    for addr, v in stale:
        cell = ws.cells.get(addr)
        ws.cells.clear_contents(cell.row, cell.column, cell.row, cell.column)
        if v is not None:
            ws.cells.get(addr).put_value(v)


def _build_sheet_from_snapshot(wb, ws, snap_sheet: dict) -> None:
    """Rebuild a source sheet from the parsed snapshot (the add-in path has no
    source file): effective values + number formats. Formula cells contribute
    their cached value — a live formula would dangle (see module docstring)."""
    styles: dict[str, object] = {}
    for c in snap_sheet.get("cells", []):
        v = effective_value(c)
        if v in (None, ""):
            continue
        addr = (c.get("address") or "").strip().upper()
        if not addr:
            continue
        cell = ws.cells.get(addr)
        cell.put_value(v)
        nf = (c.get("style") or {}).get("number_format")
        if nf:
            st = styles.get(nf)
            if st is None:
                try:
                    st = wb.create_style()
                    st.custom = nf
                except Exception:  # noqa: BLE001 — a bad format string never sinks the sheet
                    continue
                styles[nf] = st
            cell.set_style(st)


def _load_frozen_source(source_path):
    """The source workbook recalculated and frozen to values, ready to combine.
    Calculate FIRST so every computable formula has a value to keep (a formula
    cell with no cached value would otherwise freeze to blank — a real run lost
    1,583 cells that way); errors are ignored (connector functions keep their
    file-cached value or stay as-is)."""
    from aspose.cells import CalculationOptions, Workbook
    src_wb = Workbook(str(source_path))
    try:
        opts = CalculationOptions()
        opts.ignore_error = True
        src_wb.calculate_formula(opts)
    except Exception as e:  # noqa: BLE001 — freeze proceeds on the file's cached values
        logger.warning("source recalculation failed (%s) — freezing cached values", e)
    for ws in src_wb.worksheets:
        _formulas_to_values(ws)
    return src_wb


def _decorate(ws) -> None:
    """Mark an imported sheet: visible (a hidden source defeats the point) and
    tab-tinted. Cosmetic best-effort."""
    try:
        ws.is_visible = True
        from aspose.pydrawing import Color
        ws.tab_color = Color.from_argb(255, 191, 205, 224)
    except Exception:  # noqa: BLE001
        pass


def _import_source_sheets(wb, name_map: dict[str, str], snap_sheets: dict[str, dict],
                          src_wb) -> list[str]:
    """Append the source sheets in ``name_map`` to ``wb`` under their planned
    names. With the real source workbook (upload path, already frozen to
    values) the sheets arrive via ``Workbook.combine`` — the ONLY Aspose path
    that carries the full style table (fonts, fills, widths, themes; a
    cross-workbook ``Worksheet.copy`` silently drops all of it). Without one
    (add-in path) each sheet is rebuilt from the snapshot: values + number
    formats, no styling — the best the serialized form carries."""
    added: list[str] = []
    if src_wb is not None:
        pre_names = [w.name for w in wb.worksheets]
        try:
            # combine() imports every sheet, so trim the source copy to the
            # planned set first (safe: it's a frozen values-only copy).
            keep = {k.strip().lower() for k in name_map}
            order = [w.name for w in src_wb.worksheets if w.name.strip().lower() in keep]
            for nm in [w.name for w in src_wb.worksheets]:
                if nm.strip().lower() not in keep and len(src_wb.worksheets) > 1:
                    src_wb.worksheets.remove_at(nm)
            by_lower = {k.strip().lower(): v for k, v in name_map.items()}
            n0 = len(wb.worksheets)
            wb.combine(src_wb)
            for i, orig in enumerate(order):
                ws = wb.worksheets[n0 + i]
                ws.name = by_lower[orig.strip().lower()]
                _decorate(ws)
                added.append(ws.name)
            return added
        except Exception as e:  # noqa: BLE001 — degrade to snapshot rebuild
            logger.warning("source combine failed (%s) — rebuilding from snapshot", e)
            # a half-combined import must not survive: drop EVERY sheet the
            # attempt appended (combine may have added auto-renamed sheets
            # before the failure, not just the ones tracked in `added`)
            for nm in [w.name for w in wb.worksheets]:
                if nm not in pre_names:
                    try:
                        wb.worksheets.remove_at(nm)
                    except Exception:  # noqa: BLE001
                        pass
            added = []

    for orig, new in name_map.items():
        res = wb.worksheets.add(new)      # binding returns Worksheet (older: index)
        tgt = wb.worksheets[res] if isinstance(res, int) else res
        _build_sheet_from_snapshot(wb, tgt, snap_sheets.get(orig) or {})
        _decorate(tgt)
        added.append(new)
    return added


def attach_source_links(wb, filled: list[FilledCell], links: list[CellLink],
                        source_snapshot: dict, source_path=None) -> dict:
    """Mutate the ALREADY-FILLED workbook into its traceable form: import the
    source sheets the fills cite, then replace each filled cell's value with
    the formula that derives it. Called AFTER the values deliverable's bytes
    are frozen — the same in-memory workbook then serves both saves.

    Returns stats: sheets_added, formula_cells, fallback_cells, verified,
    mismatches (sample). Never raises for a single bad cell — that cell keeps
    its raw value and is counted."""
    from aspose.cells import CalculationOptions

    link_by: dict[tuple[str, str], CellLink] = {}
    for lk in links:
        link_by.setdefault((lk.template_sheet, (lk.template_cell or "").upper()), lk)

    snap_sheets = {s.get("name", ""): s for s in source_snapshot.get("sheets", [])}
    canon = {n.strip().lower(): n for n in snap_sheets}

    def canon_sheet(name: str) -> str:
        return canon.get((name or "").strip().lower()) or (name or "").strip()

    pairs: list[tuple[FilledCell, CellLink]] = []
    needed: list[str] = []
    for fc in filled:
        lk = link_by.get((fc.template_sheet, (fc.template_cell or "").upper()))
        if lk is None:
            continue                   # a fill with no surviving link keeps its value
        pairs.append((fc, lk))
        for nm in cited_sheets(lk):
            cn = canon_sheet(nm)
            if cn not in needed:
                needed.append(cn)
    if not pairs:
        return {"sheets_added": [], "formula_cells": 0, "fallback_cells": 0,
                "verified": True, "note": "no filled cells to link"}

    # With the real file, import the WHOLE pack (every non-empty sheet) — the
    # user's source data travels with the deliverable, not just the sheets the
    # links happen to cite. Snapshot path: cited sheets only (no styling to
    # preserve, and the snapshot may include noise sheets).
    src_wb = None
    if source_path is not None:
        try:
            src_wb = _load_frozen_source(source_path)
        except Exception as e:  # noqa: BLE001 — degrade to snapshot rebuild
            logger.warning("could not reopen source workbook (%s) — snapshot rebuild", e)
    import_names = list(needed)
    if src_wb is not None:
        seen_l = {n.strip().lower() for n in import_names}
        for w in src_wb.worksheets:
            if w.name.strip().lower() not in seen_l and w.cells.max_data_row >= 0:
                import_names.append(w.name)
                seen_l.add(w.name.strip().lower())

    name_map = plan_source_sheet_names(import_names, [w.name for w in wb.worksheets])
    lower_map = {k.strip().lower(): v for k, v in name_map.items()}

    def resolve(name: str) -> str | None:
        return lower_map.get(canon_sheet(name).lower())

    sheets_added = _import_source_sheets(wb, name_map, snap_sheets, src_wb)

    opts = CalculationOptions()
    try:
        opts.ignore_error = True
    except Exception:  # noqa: BLE001
        pass

    ws_by_name = {w.name: w for w in wb.worksheets}
    formula_cells = fallback_cells = 0
    verified = True
    mismatches: list[dict] = []
    for fc, lk in pairs:
        ws = ws_by_name.get(fc.template_sheet)
        if ws is None:
            continue                   # already surfaced as a write_failure upstream
        f = formula_for(lk, resolve, is_text=isinstance(fc.value, str))
        if f is None:
            fallback_cells += 1        # raw value (already in place) stands
            continue
        cell = ws.cells.get(fc.template_cell)
        cell.formula = f
        ok = None
        try:
            # per-cell calc: precedents are static values on the imported
            # sheets, so nothing else (esp. connector formulas) is recomputed.
            cell.calculate(opts)
            ok = _values_agree(cell.value, fc.value)
        except Exception as e:  # noqa: BLE001 — calc unavailable ≠ formula wrong
            logger.warning("per-cell verification unavailable (%s) — linked formulas unverified", e)
            verified = False
        if ok is False:
            if len(mismatches) < 20:
                mismatches.append({"cell": f"{fc.template_sheet}!{fc.template_cell}",
                                   "formula": f, "expected": fc.value,
                                   "computed": cell.value})
            ws.cells.clear_contents(cell.row, cell.column, cell.row, cell.column)
            ws.cells.get(fc.template_cell).put_value(fc.value)
            fallback_cells += 1
        else:
            formula_cells += 1

    stats: dict = {"sheets_added": sheets_added, "formula_cells": formula_cells,
                   "fallback_cells": fallback_cells, "verified": verified}
    if mismatches:
        stats["mismatches"] = mismatches
    return stats
