"""Build the grounded TEXT GRID half of a sheet view (Layer 3).

Renders a snapshot sheet into the compact row-major grid the per-sheet system
prompt describes. Format per occupied row:

    r{row}: A=value | C=*label | D==FORMULA

Marker placement (must match prompts.SYSTEM):
  - prefix `*` (bold) and `›N ` (indent depth) BEFORE the value
  - suffix `[in]` / `[fill]` / `[unlocked]` / `[mrg:RANGE]` AFTER the value
    (space-separated); `[fill]` = a visible solid fill outside the input palette
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

import os as _os

# Cell body cap: 90 keeps the tails of verbose adjustment/definition labels the
# 60-char cap used to cut (exactly the text this product cares about).
_MAX_BODY = 90
_VINPUT_BUDGET = 300           # cap on injected empty-validation cells per sheet


def _env_int(name: str, default: int) -> int:
    try:
        return int(_os.environ.get(name, default))
    except ValueError:
        return default
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


def _fmt(v) -> str:
    """Render a cell value for the grid: an ISO datetime ('2025-01-31T00:00:00' —
    what a formula date header caches) shows as its date; everything else is
    stringified and trimmed."""
    if hasattr(v, "year") and hasattr(v, "month") and hasattr(v, "day"):
        return f"{v.year:04d}-{v.month:02d}-{v.day:02d}"
    s = str(v).strip().replace("\n", " ")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T[\d:.]+$", s)
    if m:
        s = m.group(1)
    return s if len(s) <= _MAX_BODY else s[:_MAX_BODY] + "…"


def _body(c: dict) -> str:
    formula = c.get("formula")
    if formula:
        # A computed cell shows BOTH: its RESULT first (the model needs real values —
        # a formula date header caches its date), then the formula in braces (the
        # model needs the LOGIC — sign conventions, cross-sheet flows, dependencies
        # are all read from formula text). Value first so truncation only ever eats
        # the formula tail, never the result.
        f = str(formula)
        cv = c.get("cached_value")
        if cv in (None, ""):
            return f if len(f) <= _MAX_BODY else f[:_MAX_BODY] + "…"
        v = _fmt(cv)
        room = _MAX_BODY - len(v) - 4          # overhead: '=', space, braces
        if room < 8:                            # value ate the budget — keep it, drop the formula
            return f"={v}"
        if len(f) > room:
            f = f[:room] + "…"
        return f"={v} {{{f}}}"
    val = c.get("value")
    if val is None:
        return ""
    return _fmt(val)


# theme background slots / the 'automatic' indexed colours — render as the sheet
# background, not as author signal (they used to flood [fill] markers).
_BACKGROUND_FILLS = {"theme:0", "theme:1", "indexed:64", "indexed:65"}


def _has_visible_fill(fill: str | None) -> bool:
    """A fill colour that actually shows: set, and not white/near-white ARGB
    (whole-sheet white washes are background, not signal — all channels >= 0xF0
    reads as a wash). Other indexed/theme fills count — they render as real
    colours even though the hex is unresolved."""
    if not fill:
        return False
    if fill in _BACKGROUND_FILLS:
        return False
    if fill.startswith(("indexed:", "theme:")):
        return True
    rgb = fill.lower()
    if len(rgb) == 8:
        rgb = rgb[2:]
    try:
        r, g, b = (int(rgb[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return rgb != "ffffff"
    return not (r >= 0xF0 and g >= 0xF0 and b >= 0xF0)


def _cell_token(c: dict, anchor: dict) -> str:
    st = c.get("style") or {}
    lvl = st.get("indent_level") or 0
    prefix = ("*" if st.get("bold") else "") + (f"›{lvl} " if lvl else "")
    core = f"{prefix}{_body(c)}"
    markers: list[str] = []
    fill = st.get("fill_color")
    if is_input_fill(fill):
        markers.append("[in]")
    elif _has_visible_fill(fill):
        # A solid fill OUTSIDE the input palette still encodes author intent
        # (section shading, hardcode-vs-formula colour schemes) — surface it as a
        # bare [fill] (no hex: token budget) so it isn't invisible to the model.
        markers.append("[fill]")
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


def build_text_grid(sheet: dict, max_rows: int | None = None, max_cols: int | None = None) -> str:
    # Window caps are env-tunable: rows past the window are INVISIBLE to
    # understanding (a note is emitted, but the data is gone) — raise for big
    # templates. Defaults: 350 rows (was 250), 80 cols.
    if max_rows is None:
        max_rows = _env_int("TEMPO_GRID_MAX_ROWS", 350)
    if max_cols is None:
        max_cols = _env_int("TEMPO_GRID_MAX_COLS", 80)
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
