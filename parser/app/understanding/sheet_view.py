"""Build the grounded TEXT GRID half of a sheet view (Layer 3).

Renders a snapshot sheet into the compact row-major grid the per-sheet system
prompt describes. Format per occupied row:

    r{row}: A=value | C=*label | D==FORMULA

Marker placement (must match prompts.SYSTEM):
  - prefix `*` (bold) and `›N ` (indent depth) BEFORE the value
  - suffix `[in]` / `[unlocked]` / `[mrg:RANGE]` AFTER the value (space-separated)
  - leading `=` is a formula; trailing `…` means the formula was truncated
  - a token with no value (e.g. `E=[in]`) is an EMPTY cell that sits in a
    data-validation range — a prime input-field candidate

Empty validation cells and merged markers come straight from the snapshot
(no re-parse). Pure function — no Aspose, no I/O.
"""

from __future__ import annotations

import re

from app.raw_extraction.cell_analyzer import is_input_fill
from app.raw_extraction.column_utils import column_index, column_letter

_MAX_BODY = 60
_VINPUT_BUDGET = 300           # cap on injected empty-validation cells per sheet
_RANGE_RE = re.compile(r"^\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?$")


def _range_bounds(ref: str) -> tuple[int, int, int, int] | None:
    m = _RANGE_RE.match(ref.strip())
    if not m:
        return None
    c1 = column_index(m.group(1))
    r1 = int(m.group(2))
    if m.group(3):
        c2, r2 = column_index(m.group(3)), int(m.group(4))
    else:
        c2, r2 = c1, r1
    return (min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))


def _body(c: dict) -> str:
    formula = c.get("formula")
    if formula:
        s = str(formula)
        return s if len(s) <= _MAX_BODY else s[:_MAX_BODY] + "…"
    val = c.get("value")
    if val is None:
        return ""
    s = str(val).strip().replace("\n", " ")
    return s if len(s) <= _MAX_BODY else s[:_MAX_BODY] + "…"


def _cell_token(c: dict, anchor: dict) -> str:
    st = c.get("style") or {}
    lvl = st.get("indent_level") or 0
    prefix = ("*" if st.get("bold") else "") + (f"›{lvl} " if lvl else "")
    core = f"{prefix}{_body(c)}"
    markers: list[str] = []
    if is_input_fill(st.get("fill_color")):
        markers.append("[in]")
    if st.get("is_locked") is False:
        markers.append("[unlocked]")
    key = (c["row"], c["col"])
    if key in anchor:
        markers.append(f"[mrg:{anchor[key]}]")
    return _assemble(c["col"], core, markers)


def _vinput_token(row: int, col: int) -> str:
    return _assemble(col, "", ["[in]"])


def _assemble(col: int, core: str, markers: list[str]) -> str:
    letter = column_letter(col)
    m = " ".join(markers)
    if core and m:
        return f"{letter}={core} {m}"
    if m:
        return f"{letter}={m}"
    return f"{letter}={core}"


def _validation_empty_cells(sheet: dict, occupied: set, max_cols: int) -> set:
    """Empty cells inside data-validation ranges — author-defined inputs that
    carry no value yet and would otherwise be invisible to the model."""
    injected: set[tuple[int, int]] = set()
    budget = _VINPUT_BUDGET
    for v in sheet.get("data_validations", []):
        for sub in (v.get("cell_range") or "").split(","):
            b = _range_bounds(sub)
            if not b:
                continue
            r1, c1, r2, c2 = b
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    if c > max_cols or (r, c) in occupied or (r, c) in injected:
                        continue
                    injected.add((r, c))
                    budget -= 1
                    if budget <= 0:
                        return injected
    return injected


def build_text_grid(sheet: dict, max_rows: int = 250, max_cols: int = 80) -> str:
    name = sheet["name"]
    protected = bool(sheet.get("is_protected"))
    cells = sheet.get("cells", [])
    anchor = {(m["min_row"], m["min_col"]): m["range"] for m in sheet.get("merged_ranges", [])}
    occupied = {(c["row"], c["col"]) for c in cells}

    by_row: dict[int, list] = {}
    max_seen_col = 0
    for c in cells:
        col = c["col"]
        max_seen_col = max(max_seen_col, col)
        if col > max_cols:
            continue
        by_row.setdefault(c["row"], []).append(("cell", c))

    injected = _validation_empty_cells(sheet, occupied, max_cols)
    for (r, c) in injected:
        by_row.setdefault(r, []).append(("vinput", (r, c)))

    rows_sorted = sorted(by_row)
    truncated_rows = len(rows_sorted) > max_rows
    rows_sorted = rows_sorted[:max_rows]

    header = [
        f"# Sheet: {name}{'  (PROTECTED)' if protected else ''}",
        f"# used range {sheet.get('used_max_row', 0)}r x {sheet.get('used_max_col', 0)}c | "
        f"shown {len(rows_sorted)} occupied rows"
        + (f" (+{len(injected)} empty validation cells)" if injected else "")
        + f" | cols<={min(max_cols, max_seen_col) or max_cols}",
    ]
    if truncated_rows:
        header.append(f"# NOTE: {len(by_row) - max_rows} further occupied rows omitted (window).")
    if max_seen_col > max_cols:
        header.append(f"# NOTE: columns beyond {column_letter(max_cols)} omitted "
                      f"(sheet reaches {column_letter(max_seen_col)}).")

    grp = sheet.get("row_group_levels", {})  # keys are strings (JSON)
    lines: list[str] = []
    for r in rows_sorted:
        entries = sorted(by_row[r], key=lambda e: (e[1]["col"] if e[0] == "cell" else e[1][1]))
        toks = [
            _cell_token(data, anchor) if kind == "cell" else _vinput_token(*data)
            for kind, data in entries
        ]
        g = grp.get(str(r))
        prefix = f"r{r}[grp:{g}]: " if g else f"r{r}: "
        lines.append(prefix + " | ".join(toks))

    return "\n".join(header) + "\n" + "\n".join(lines)
