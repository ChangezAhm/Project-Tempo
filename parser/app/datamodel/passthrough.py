"""Defaulted-input detection: a visible formula cell that is a thin *display* of a
single backend input point.

Background (Flash-style connector templates): the cell a human fills on the front
sheet is often a formula, e.g. ``PL!AD20 = TODECIMAL(IFBLANK(_PL!AD20,""),fxVal)`` —
a currency-converting wrapper over a single backend cell ``_PL!AD20`` that is itself
a connector (CX_GET) fetching the value. The naive rule "a formula is a computed
output" wrongly discards these — they are the real inputs, one layer behind the cell.

The safe discriminator is STRUCTURAL, not a function whitelist: a cell that
references EXACTLY ONE data cell cannot be an aggregate (needs a range / ≥2 refs) or
a combination (a ratio needs 2) — it can only echo or unit-transform that one cell.
So "resolves through single-cell references to a terminal INPUT POINT (a connector,
or a blank/literal in a data column)" flags defaulted inputs without ever flagging a
subtotal. Scalar parameters (named ranges like ``fxVal_normal``, ``$L$35`` sign
flags in non-data columns) are not data cells and never count as the single ref that
matters, because the terminal must land on a *data-column* input point.

The backend cell a passthrough lands on is its BACKING store, not an independent
input: it is suppressed so population writes the front cell, not the backend. This
also dedupes multi-hop chains (J → Z → _PL) down to the single outermost cell.

Keys are ``(sheet, row, col)`` throughout, matching ``derive.py``'s snapshot maps.
Pure and offline-testable: no Aspose, no Supabase.
"""

from __future__ import annotations

import re
from collections import defaultdict

from app.datamodel.derive import _CONNECTOR

# A cell reference inside a formula. QUAL = sheet-qualified ('Sheet 1'!A1 or _PL!A1);
# BARE = same-sheet A1. The trailing (?![\w(]) rejects function names (FOO() ) and
# longer tokens, so LOG10( / named ranges never masquerade as a cell.
_RANGE = re.compile(r"(?:'[^']+'|[A-Za-z_][\w.]*)?!?\$?[A-Z]{1,3}\$?\d+\s*:\s*\$?[A-Z]{1,3}\$?\d+")
_QUAL = re.compile(r"(?:'([^']+)'|([A-Za-z_][\w.]*))!\$?([A-Z]{1,3})\$?(\d+)(?![\w(])")
_BARE = re.compile(r"(?<![\w!.$])\$?([A-Z]{1,3})\$?(\d+)(?![\w(])")


def _col_to_idx(col: str) -> int:
    x = 0
    for ch in col:
        x = x * 26 + (ord(ch) - 64)
    return x - 1


def is_connector(formula: str | None) -> bool:
    return bool(formula and _CONNECTOR.search(formula))


def extract_refs(formula: str, cur_sheet: str) -> tuple[bool, set[tuple[str, int, int]]]:
    """Return (has_range, {(sheet, row, col)}). A range short-circuits to
    has_range=True with no singles — a ranged formula is never a passthrough."""
    if _RANGE.search(formula):
        return True, set()
    refs: set[tuple[str, int, int]] = set()
    for m in _QUAL.finditer(formula):
        sheet = m.group(1) or m.group(2)
        refs.add((sheet, int(m.group(4)) - 1, _col_to_idx(m.group(3))))
    for m in _BARE.finditer(_QUAL.sub(" ", formula)):
        refs.add((cur_sheet, int(m.group(2)) - 1, _col_to_idx(m.group(1))))
    return False, refs


def build_data_cols(cell_formula: dict[tuple[str, int, int], str]) -> dict[str, set[int]]:
    """Columns holding ≥1 connector, per sheet — the workbook's own signal for
    "this column carries fetched data" (so a blank there is a raw input slot)."""
    out: dict[str, set[int]] = defaultdict(set)
    for (sheet, _r, c), f in cell_formula.items():
        if is_connector(f):
            out[sheet].add(c)
    return out


def _is_input_point(key: tuple[str, int, int], cell_formula, cell_val, data_cols) -> bool:
    """A terminal input: a connector cell, or a blank/literal in a data column. A
    non-connector formula in a data column is NOT terminal — keep resolving through
    it (it may itself be a passthrough) or reject."""
    f = cell_formula.get(key)
    if is_connector(f):
        return True
    if f:
        return False
    sheet, _r, c = key
    if c in data_cols.get(sheet, set()):
        v = cell_val.get(key)
        return v is None or v == "" or isinstance(v, (int, float))
    return False


def resolve_passthrough(key, cell_formula, cell_val, data_cols, max_depth=4):
    """If ``key`` is a formula that displays a single backend input point, return the
    chain of cells it passes THROUGH (intermediates + terminal) — these are backing
    cells to suppress. Return None if ``key`` is not a passthrough (a range, a
    combination, or it never lands on an input point). ``key`` itself is never
    included in the returned backing set."""
    backing: list[tuple[str, int, int]] = []
    cur = key
    seen = {key}
    for _ in range(max_depth):
        f = cell_formula.get(cur)
        if not f or is_connector(f):
            # A connector at `cur` is only a terminal when cur != key (key being a
            # connector is the existing connector pass's job, not ours).
            return backing if (cur is not key and is_connector(f)) else None
        has_range, refs = extract_refs(f, cur[0])
        if has_range or len(refs) != 1:
            return None
        nxt = next(iter(refs))
        if nxt in seen:
            return None
        seen.add(nxt)
        if _is_input_point(nxt, cell_formula, cell_val, data_cols):
            backing.append(nxt)
            return backing
        backing.append(nxt)
        cur = nxt
    return None


def find_passthrough_inputs(sheets, cell_formula, cell_val, data_cols=None):
    """Scan ``sheets`` (visible input sheets) for defaulted-input cells.

    Returns (inputs, backing):
      - inputs: {(sheet, row, col)} — front cells to enumerate as fillable inputs.
      - backing: {(sheet, row, col)} — cells any passthrough lands on/through; these
        are suppressed so we write the front, not the backend (and dedupe chains).
    A flagged cell that is itself some other passthrough's backing is dropped from
    inputs (only the outermost cell in a chain survives)."""
    if data_cols is None:
        data_cols = build_data_cols(cell_formula)
    flagged: set[tuple[str, int, int]] = set()
    backing: set[tuple[str, int, int]] = set()
    want = set(sheets)
    for (sheet, r, c), f in cell_formula.items():
        if sheet not in want or is_connector(f):
            continue
        chain = resolve_passthrough((sheet, r, c), cell_formula, cell_val, data_cols)
        if chain is not None:
            flagged.add((sheet, r, c))
            # Suppress only non-connector RELAY cells (e.g. an intra-sheet Z-column
            # intermediate). A connector is always an independently-valid input the
            # connector pass owns and the owner may mark directly (KPI) — never
            # suppress it, or those inputs vanish.
            backing.update(k for k in chain if not is_connector(cell_formula.get(k)))
    return flagged - backing, backing
