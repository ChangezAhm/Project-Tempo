"""Connector topology facts: formula reference extraction and the CX_PUSH
entry-cell inventory.

Two jobs, both pure workbook FACTS (no LLM, no I/O, no priors):

1. ``is_multi_input(formula)`` — the structural CONSTRAINT of the write-semantics
   authority model (docs/Fill-Plan-Architecture.md §1, plan: write-semantics fix):
   a formula that reads a RANGE or ≥2 distinct cells is a computation and is
   never converted into a fillable input, whatever the LLM claims. (The looser
   ``aggregation._is_aggregation`` misses ratios and AVERAGE/MAX-style range
   functions — the pinned "a ratio is never an input" invariant lives here.)

2. ``push_targets(cell_formula)`` — cells a CX_PUSH formula READS. The template
   pushes what the user types, so a push-referenced cell is an entry cell by the
   workbook's own declaration: decisive evidence independent of any LLM claim.

Coordinates are 1-BASED throughout, matching derive's cell maps — the deleted
passthrough module's 0-based ``extract_refs`` silently resolved every reference
one row up and one column left (the Dec-22 cliff).
"""

from __future__ import annotations

import re

from app.population.aggregation import _is_aggregation
from app.raw_extraction.column_utils import column_index

# A cell reference inside a formula. QUAL = sheet-qualified ('Sheet 1'!A1 or
# _PL!A1); BARE = same-sheet A1. The trailing (?![\w(]) rejects function names
# (LOG10( ) and longer tokens, so named ranges never masquerade as a cell.
_RANGE = re.compile(r"(?:'[^']+'|[A-Za-z_][\w.]*)?!?\$?[A-Z]{1,3}\$?\d+\s*:\s*\$?[A-Z]{1,3}\$?\d+")
_QUAL = re.compile(r"(?:'([^']+)'|([A-Za-z_][\w.]*))!\$?([A-Z]{1,3})\$?(\d+)(?![\w(])")
_BARE = re.compile(r"(?<![\w!.$])\$?([A-Z]{1,3})\$?(\d+)(?![\w(])")

# The push CALL shape — not the bare token, so a formula merely mentioning
# CX_PUSH in a string never claims entry cells.
_PUSH = re.compile(r"(?i)(?:_xldudf_)?CX[._]PUSH\s*\(")

# Ranges inside a push expand to at most this many cells (a push over a whole
# column is normal; an unbounded expansion is not).
_MAX_PUSH_RANGE_CELLS = 2000

# A push whose formula references MORE single cells than this is composing a
# label / evaluating conditions (PROD's label-push soup references dozens of
# control cells) — its single refs are not entry declarations. Ranges always
# count: pushing a month-row range is the normal value-push shape.
_MAX_PUSH_SINGLE_REFS = 4


def extract_refs(formula: str, cur_sheet: str) -> tuple[bool, set[tuple[str, int, int]]]:
    """(has_range, {(sheet, row, col)}) — single-cell references, 1-BASED.
    A range short-circuits to has_range=True with no singles."""
    if _RANGE.search(formula):
        return True, set()
    refs: set[tuple[str, int, int]] = set()
    stripped = formula
    for m in _QUAL.finditer(formula):
        sheet = m.group(1) or m.group(2)
        refs.add((sheet, int(m.group(4)), column_index(m.group(3))))
    stripped = _QUAL.sub(" ", stripped)
    for m in _BARE.finditer(stripped):
        refs.add((cur_sheet, int(m.group(2)), column_index(m.group(1))))
    return False, refs


def is_multi_input(formula: str | None) -> bool:
    """True when the formula is a COMPUTATION over several inputs — a range, ≥2
    distinct cells, or an aggregation shape — and must never become a fillable
    input. A single-cell display/transform wrapper (=TODECIMAL(IFBLANK(X,"")…))
    returns False."""
    if not formula:
        return False
    has_range, refs = extract_refs(formula, "")
    return has_range or len(refs) >= 2 or _is_aggregation(formula)


def _expand_range(rng: str, cur_sheet: str) -> set[tuple[str, int, int]]:
    """Cells of one 'Sheet!A1:B9' / 'A1:B9' range token, capped."""
    sheet = cur_sheet
    body = rng
    if "!" in rng:
        sh, body = rng.rsplit("!", 1)
        sheet = sh.strip().strip("'")
    m = re.match(r"\$?([A-Z]{1,3})\$?(\d+)\s*:\s*\$?([A-Z]{1,3})\$?(\d+)", body.strip())
    if not m:
        return set()
    c1, r1 = column_index(m.group(1)), int(m.group(2))
    c2, r2 = column_index(m.group(3)), int(m.group(4))
    lo_r, hi_r = min(r1, r2), max(r1, r2)
    lo_c, hi_c = min(c1, c2), max(c1, c2)
    if (hi_r - lo_r + 1) * (hi_c - lo_c + 1) > _MAX_PUSH_RANGE_CELLS:
        return set()
    return {(sheet, r, c) for r in range(lo_r, hi_r + 1) for c in range(lo_c, hi_c + 1)}


def push_targets(cell_formula: dict[tuple[str, int, int], str]) -> set[tuple[str, int, int]]:
    """Every cell some CX_PUSH formula reads — the workbook's own declaration of
    its entry cells. Keys/values 1-based, matching derive's maps."""
    out: set[tuple[str, int, int]] = set()
    for (sheet, _r, _c), f in cell_formula.items():
        if not f or not _PUSH.search(f):
            continue
        for rng in _RANGE.findall(f):
            out |= _expand_range(rng, sheet)
        _has, singles = extract_refs(_RANGE.sub(" ", f), sheet)
        # FRONT/BACKEND PAIR rule first: a push reading both X and OtherSheet!X
        # (same row+col, different sheet) is comparing the entry cell to its
        # mirror before pushing — the copy on the push's OWN sheet is the
        # declared entry; condition/label refs never pair. Only a pair-less
        # arg-style push (CX_PUSH(...,AD50,...)) falls back to its few singles;
        # a many-ref pair-less formula is label/condition soup and drops.
        by_rc: dict[tuple[int, int], set[str]] = {}
        for (s, r, c) in singles:
            by_rc.setdefault((r, c), set()).add(s)
        pairs = {(sheet, r, c) for (r, c), sheets_of in by_rc.items()
                 if len(sheets_of) > 1 and sheet in sheets_of}
        if pairs:
            out |= pairs
        elif len(singles) <= _MAX_PUSH_SINGLE_REFS:
            out |= singles
    return out


def push_entry_summary(cell_formula: dict[tuple[str, int, int], str], sheet: str) -> str | None:
    """Compact per-sheet description of push-read cells ('cols AD-BN, rows
    20-41') for the understanding hints — how the model learns of entry columns
    that sit beyond the text grid's column cap."""
    from app.raw_extraction.column_utils import column_letter

    cells = [(r, c) for (sh, r, c) in push_targets(cell_formula) if sh == sheet]
    if not cells:
        return None
    rows = sorted({r for r, _c in cells})
    cols = sorted({c for _r, c in cells})
    return (f"cols {column_letter(cols[0])}-{column_letter(cols[-1])}, "
            f"rows {rows[0]}-{rows[-1]} ({len(cells)} cells)")
