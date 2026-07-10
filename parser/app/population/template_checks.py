"""Post-fill template verification — "manipulate the template and see what happens".

After populate writes values, the workbook is RECALCULATED (Aspose) and the
template's OWN check cells (a Checks sheet, tie-outs, covenant tests, OK/ERROR
flags) are read back. A template that ships its own validation deserves to have
it consulted — before this module, filled workbooks were saved with stale
computed values and nobody looked at the checks.

Zero-LLM and deterministic throughout. Discovery is layered:
  (a) check-named sheets           — every formula cell with a bool/token/number cache
  (b) L3 sections typed reconciliation/covenant — formula cells inside their range
  (c) formula shape anywhere       — boolean caches, pass/fail token caches, or
                                     IF(..,"OK","ERROR")-shaped formulas

Classification honesty: a connector function (CX_GET…) cannot compute outside its
host system — under calc it becomes #NAME? and poisons dependents — so a check
whose after-value is an error is `not_computable`, never `fail`. The deliverable
bytes are saved BEFORE calculation for the same reason (see run.render_filled).
"""

from __future__ import annotations

import logging
import re

from app.population.catalogue import a1_to_rowcol

logger = logging.getLogger(__name__)

_CHECK_SHEET = re.compile(r"(?i)\b(checks?|recon\w*|tie.?outs?|covenants?|valid\w*|integrity|qc|audit)\b")
_CHECK_SECTIONS = {"reconciliation", "covenant"}
_PASS_TOKENS = {"ok", "pass", "passed", "true", "yes", "✓", "✔"}
_FAIL_TOKENS = {"error", "fail", "failed", "false", "no", "mismatch", "err", "✗", "✘"}
_OK_ERROR_SHAPE = re.compile(r'(?i)IF\s*\(.*"\s*(ok|error|pass|fail|passed|failed|yes|no|true|false)\s*"')
_CONNECTOR = re.compile(r"(?i)CX_GET|CVC\.GET|GETPIVOTDATA|CUBEVALUE|CUBEMEMBER")
_ERRORISH = re.compile(r"^#\w")   # "#NAME?", "#REF!", "#DIV/0!", …


def _token(v) -> str | None:
    if isinstance(v, str):
        return v.strip().casefold() or None
    return None


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _bounds(rng: str | None) -> tuple[int, int, int, int] | None:
    """'B5:D8' -> (r1, c1, r2, c2); single cells too."""
    if not rng:
        return None
    parts = str(rng).replace("$", "").split(":")
    a = a1_to_rowcol(parts[0])
    b = a1_to_rowcol(parts[1]) if len(parts) > 1 else a
    if not a or not b:
        return None
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))


def _kind_of(cell: dict) -> str | None:
    """What kind of check a formula cell looks like, from its cached value/shape.
    None when it doesn't look like a check at all."""
    formula = cell.get("formula") or (cell.get("value") if str(cell.get("value", "")).startswith("=") else "")
    if formula and _CONNECTOR.search(str(formula)):
        return None                      # a connector feed is not a check
    cv = cell.get("cached_value")
    if isinstance(cv, bool):
        return "boolean"
    t = _token(cv)
    if t and (t in _PASS_TOKENS or t in _FAIL_TOKENS):
        return "ok_error"
    if formula and _OK_ERROR_SHAPE.search(str(formula)):
        return "ok_error"
    return None


def collect_check_cells(snapshot: dict, understanding: dict | None = None,
                        *, max_cells: int = 500) -> list[dict]:
    """Deterministic check discovery over the TEMPLATE snapshot (+ optional L3
    understanding). Returns [{sheet, cell, row, col, kind, origin, label, before}]
    — `before` from the snapshot cache (re-read live at render time)."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(sheet: str, c: dict, kind: str, origin: str) -> None:
        addr = (c.get("address") or "").upper()
        if not addr or (sheet, addr) in seen or len(out) >= max_cells:
            return
        seen.add((sheet, addr))
        out.append({"sheet": sheet, "cell": addr, "row": c.get("row"), "col": c.get("col"),
                    "kind": kind, "origin": origin,
                    "label": f"{sheet}!{addr}",
                    "before": c.get("cached_value", c.get("value"))})

    # per-sheet section ranges typed reconciliation/covenant, from the understanding
    section_ranges: dict[str, list[tuple[int, int, int, int]]] = {}
    for srow in (understanding or {}).get("sheets", []) or []:
        u = srow.get("understanding") or {}
        for sec in u.get("sections", []) or []:
            st = (sec.get("section_type") or "").lower()
            title = sec.get("title") or ""
            if st in _CHECK_SECTIONS or _CHECK_SHEET.search(title):
                b = _bounds(sec.get("cell_range"))
                if b:
                    section_ranges.setdefault(srow.get("sheet_name") or "", []).append(b)

    for s in snapshot.get("sheets", []):
        name = s.get("name") or ""
        named = bool(_CHECK_SHEET.search(name))
        ranges = section_ranges.get(name, [])
        for c in s.get("cells", []):
            is_formula = bool(c.get("formula")) or str(c.get("value", "")).startswith("=")
            if not is_formula:
                continue
            kind = _kind_of(c)
            in_section = any(r1 <= (c.get("row") or 0) <= r2 and c1 <= (c.get("col") or 0) <= c2
                             for (r1, c1, r2, c2) in ranges)
            if named or in_section:
                # inside a declared check surface, numeric tie-outs count too
                k = kind or ("tie_out" if _is_num(c.get("cached_value")) else None)
                if k:
                    add(name, c, k, "check_sheet" if named else "check_section")
            elif kind:
                add(name, c, kind, "formula_shape")
    return out


def classify_check(before, after, *, kind: str, tolerance: float = 0.5) -> str:
    """'pass' | 'fail' | 'not_computable' | 'indeterminate'. Error after-values are
    NOT failures — a connector-fed check simply can't compute outside its system."""
    _ = before
    if isinstance(after, str) and _ERRORISH.match(after.strip()):
        return "not_computable"
    if kind == "boolean":
        if after is True:
            return "pass"
        if after is False:
            return "fail"
        return "indeterminate"
    if kind == "ok_error":
        t = _token(after)
        if t in _PASS_TOKENS:
            return "pass"
        if t in _FAIL_TOKENS:
            return "fail"
        return "indeterminate"
    if kind == "tie_out":
        if _is_num(after):
            return "pass" if abs(after) <= tolerance else "fail"
        return "indeterminate"
    return "indeterminate"


def evaluate_checks(wb, checks: list[dict]) -> list[dict]:
    """Read the CALCULATED workbook's after-values and classify each check.
    ``wb`` is an aspose.cells.Workbook post-calculate_formula."""
    ws_by_name = {w.name: w for w in wb.worksheets}
    results: list[dict] = []
    for ch in checks:
        ws = ws_by_name.get(ch["sheet"])
        if ws is None:
            continue
        cell = ws.cells.get(ch["cell"])
        after = cell.value
        if getattr(cell, "is_error_value", False):
            after = cell.string_value if isinstance(cell.string_value, str) else "#ERR"
        status = classify_check(ch.get("before"), after, kind=ch["kind"])
        results.append({**ch, "after": after, "status": status,
                        "changed": ch.get("before") != after})
    return results


def summarize_checks(results: list[dict]) -> dict:
    from collections import Counter
    c = Counter(r["status"] for r in results)
    return {"evaluated": len(results), "passed": c.get("pass", 0),
            "failed": c.get("fail", 0), "not_computable": c.get("not_computable", 0),
            "indeterminate": c.get("indeterminate", 0),
            "changed": sum(1 for r in results if r.get("changed"))}


def checks_to_review_items(results: list[dict], source_label: str) -> list[dict]:
    """Durable review items for FAILED checks only. Keyed by sheet!cell (not the
    value), so re-runs re-bind to the same question and answers survive."""
    from app.review.items import make_item

    items: list[dict] = []
    for r in results:
        if r.get("status") != "fail":
            continue
        # question text is VALUE-FREE so the content-addressed item_key stays
        # stable across re-runs (same failing check -> same row, answers survive)
        items.append(make_item(
            source="populate", kind="judgment",
            question=(f"Template check {r['sheet']}!{r['cell']} is FAILING after this fill — "
                      "the workbook's own validation reads a fail state. Is the filled data "
                      "wrong, or is the check stale?"),
            why=(f"Recalculated after populating from {source_label}. "
                 f"Before: {str(r.get('before'))[:40]!r} → after: {str(r.get('after'))[:40]!r}."),
            affected={"sheets": [r["sheet"]], "cells": [r["cell"]]},
        ))
    return items
