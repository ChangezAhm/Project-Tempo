"""Graph-scoped double-count support for the single-use guard.

The invariant the guard must enforce is NOT "one source series → one template
cell". It is: **a source amount must never be counted twice inside the same
aggregating total.** That is the only way a reuse inflates a figure — the
Adjusted-EBITDA-adjustments case. A KPI (ARR, Headcount, Churn) shown on several
sheets shares no total and may be written as many times as needed.

This module reads the template's OWN formulas to decide. An *aggregation cell*
is a formula that combines several inputs additively (SUM/SUBTOTAL/… or a
+/- chain). Walking its precedents to the input ROWS it sums gives, per total,
the rows that feed it. Two template metrics "share a total" iff their input rows
land under a common aggregation — then, and only then, reusing one source series
across both double-counts.

Pure over the persisted snapshot (each cell carries `precedents` as ranges).
Key property: an EMPTY membership set is a POSITIVE proof of safety — the metric
feeds no total, so it can repeat freely. `metric_totals` returns None only when
the workbook has NO detectable aggregations at all (a parse gap), so the caller
can fall back to the conservative global block instead of trusting a blind pass.
"""

from __future__ import annotations

import logging
import re

from app.raw_extraction.column_utils import column_index

logger = logging.getLogger(__name__)

# A formula that sums/combines multiple inputs — the shape that can double-count.
_AGG_FN = re.compile(r"\b(?:SUM|SUBTOTAL|SUMIFS?|SUMPRODUCT|AGGREGATE)\s*\(", re.I)
# A cell/range reference, optionally sheet-qualified ("Flash!B2", "'P&L'!C5:C9").
_REF = re.compile(r"(?:'[^']+'!|[A-Za-z0-9_]+!)?\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?")
_RANGE = re.compile(r"^([A-Z]{1,3})(\d+)(?::([A-Z]{1,3})(\d+))?$")

_MAX_DEPTH = 64             # deep formula chains bottom out as leaves past here
_MAX_AGG_CELLS = 8000       # bound on aggregation cells scanned per workbook


def _is_aggregation(formula: str) -> bool:
    """True when writing the same amount into two of this formula's inputs would
    double-count. SUM-family always; otherwise a +/- chain over >=2 references
    (a bridge like 'Reported EBITDA + adj1 + adj2', or 'Debt - Cash')."""
    if not formula:
        return False
    if _AGG_FN.search(formula):
        return True
    refs = _REF.findall(formula)
    return len(refs) >= 2 and ("+" in formula or "-" in formula)


def _parse_ref(ref: str, default_sheet: str) -> tuple[str, int, int, int, int] | None:
    """'Flash!C15:C17' / 'C18' -> (sheet, r1, c1, r2, c2). Unqualified refs take
    the referencing cell's sheet."""
    ref = ref.strip()
    if "!" in ref:
        sh, rng = ref.rsplit("!", 1)
        sheet = sh.strip().strip("'")
    else:
        sheet, rng = default_sheet, ref
    m = _RANGE.match(rng.replace("$", ""))
    if not m:
        return None
    c1, r1 = column_index(m.group(1)), int(m.group(2))
    if m.group(3):
        c2, r2 = column_index(m.group(3)), int(m.group(4))
    else:
        c2, r2 = c1, r1
    return sheet, min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


def _index(snapshot: dict) -> tuple[dict, dict]:
    """Per-sheet (row,col)->cell map and (row,col)->formula-or-None."""
    by_rc: dict[str, dict[tuple[int, int], dict]] = {}
    for s in snapshot.get("sheets", []) or []:
        name = s.get("name") or ""
        m = by_rc.setdefault(name, {})
        for c in s.get("cells", []) or []:
            r, col = c.get("row"), c.get("col")
            if r is not None and col is not None:
                m[(r, col)] = c
    return by_rc, {}


def total_leaf_rows(snapshot: dict) -> dict[tuple[str, int], set[tuple[str, int]]]:
    """{(sheet, total_row) -> {(sheet, input_row), …}} — for each aggregation
    cell, the INPUT rows it transitively sums (columns collapsed: a subtotal row
    sums the same input rows in every period column)."""
    by_rc, _ = _index(snapshot)
    memo: dict[tuple[str, int, int], frozenset] = {}

    def leaves(sheet: str, row: int, col: int, depth: int, seen: frozenset) -> frozenset:
        key = (sheet, row, col)
        if key in memo:
            return memo[key]
        cell = by_rc.get(sheet, {}).get((row, col))
        formula = cell.get("formula") if cell else None
        # a non-formula cell (or one we can't descend) is an input leaf ROW
        if not formula or depth <= 0 or key in seen:
            res = frozenset({(sheet, row)})
            memo[key] = res
            return res
        acc: set[tuple[str, int]] = set()
        seen2 = seen | {key}
        for pr in cell.get("precedents") or []:
            parsed = _parse_ref(pr, sheet)
            if not parsed:
                continue
            psheet, r1, c1, r2, c2 = parsed
            src = by_rc.get(psheet, {})
            for (pr_row, pr_col), _pcell in src.items():
                if not (r1 <= pr_row <= r2 and c1 <= pr_col <= c2):
                    continue
                if (psheet, pr_row, pr_col) == key:
                    continue
                acc |= leaves(psheet, pr_row, pr_col, depth - 1, seen2)
        res = frozenset(acc) if acc else frozenset({(sheet, row)})
        memo[key] = res
        return res

    out: dict[tuple[str, int], set[tuple[str, int]]] = {}
    scanned = 0
    for sheet, cells in by_rc.items():
        for (row, col), c in cells.items():
            if not _is_aggregation(c.get("formula") or ""):
                continue
            scanned += 1
            if scanned > _MAX_AGG_CELLS:
                logger.warning("aggregation scan hit cap (%d) — some totals unmodelled", _MAX_AGG_CELLS)
                return out
            agg_id = (sheet, row)
            leaf_rows = leaves(sheet, row, col, _MAX_DEPTH, frozenset())
            # a total's own row is not one of its inputs
            out.setdefault(agg_id, set()).update(lr for lr in leaf_rows if lr != agg_id)
    return out


def _metric_key(fact: dict) -> str | None:
    return fact.get("canonical_metric") or fact.get("metric_label")


def metric_totals(facts: list[dict], snapshot: dict | None) -> dict[str, frozenset] | None:
    """metric_key -> the set of totals its cells feed. Empty set = feeds no total
    (safe to repeat). Returns None when the workbook has NO detectable
    aggregations at all, signalling the caller to fall back to the conservative
    global single-use guard rather than trust a blind pass."""
    if not snapshot:
        return None
    leaf_rows = total_leaf_rows(snapshot)
    if not leaf_rows:
        return None
    # invert: input row -> the totals that sum it
    row_to_totals: dict[tuple[str, int], set[tuple[str, int]]] = {}
    for agg_id, rows in leaf_rows.items():
        for rr in rows:
            row_to_totals.setdefault(rr, set()).add(agg_id)

    membership: dict[str, set] = {}
    for f in facts:
        key = _metric_key(f)
        if key is None:
            continue
        rc = (f.get("sheet_name"), f.get("row"))
        totals = row_to_totals.get(rc)
        if totals:
            membership.setdefault(key, set()).update(totals)
        else:
            membership.setdefault(key, set())   # present but feeds nothing = safe
    return {k: frozenset(v) for k, v in membership.items()}
