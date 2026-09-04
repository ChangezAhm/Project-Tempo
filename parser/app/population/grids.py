"""Full-visibility grids for the mapping model — the workbook, not a summary.

Every population failure traced to the same root: the mapping model was shown a
pre-chewed digest (labels + five sample values) instead of the sheets, and the
deterministic code that did the 'seeing' kept destroying structure the model
would have read natively (transposed layouts, fiscal labels, scenario columns,
magnitudes). This module renders the ACTUAL grid — address-tagged, values as
they compute, compact — so the mapper reasons the way a human analyst does:
by looking at both spreadsheets.

Trust boundary unchanged: the model still only OUTPUTS decisions (series ids,
rollup/unit/sign semantics); every address and value it acts on is verified
and read deterministically downstream. Seeing more never lets it write more.

Caps are honest, never silent: an oversized sheet degrades to a per-row
summary with an explicit banner, and the assembler reports what was reduced.
"""

from __future__ import annotations

from app.population.catalogue import _letters, effective_value

# ~120k chars ≈ 30k tokens per sheet; ~400k chars total keeps even a two-pack
# grid block inside a single call's context with room for the plan output.
SHEET_CHAR_CAP = 120_000
TOTAL_CHAR_CAP = 400_000


def _fmt_value(v, pct: bool) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        s = f"{v:.6g}"
        return s + "%" if pct else s
    s = str(v).strip()
    if not s:
        return None
    if s.startswith("="):          # formula text without a cached value — not data
        return None
    if len(s) > 40:
        s = s[:37] + "…"
    return f"'{s}'"


def sheet_grid(sheet: dict, *, char_cap: int = SHEET_CHAR_CAP) -> str:
    """One sheet as address-tagged rows: ``r5: B='FY24-Q1' E=1234.5 F=0.42%``.
    Formulas contribute their computed value (what the sheet displays). A sheet
    too big for the cap degrades to a labelled row summary, bannered."""
    name = sheet.get("name", "?")
    by_row: dict[int, list[tuple[int, str]]] = {}
    for c in sheet.get("cells", []):
        pct = "%" in ((c.get("style") or {}).get("number_format") or "")
        s = _fmt_value(effective_value(c), pct)
        if s is not None:
            by_row.setdefault(c["row"], []).append((c["col"], s))

    lines = [f"=== SHEET: {name} ==="]
    for r in sorted(by_row):
        cells = "  ".join(f"{_letters(col)}{r}={s}" for col, s in sorted(by_row[r]))
        lines.append(cells)
    grid = "\n".join(lines)
    if len(grid) <= char_cap:
        return grid

    # ROW-SUMMARY fallback — reduced, and SAYS so (no silent truncation).
    lines = [f"=== SHEET: {name} === [too large for a full grid — one line per row: "
             "first text label, then first/last values with count]"]
    for r in sorted(by_row):
        cells = sorted(by_row[r])
        label = next((s for _c, s in cells if s.startswith("'")), None)
        vals = [(col, s) for col, s in cells if not s.startswith("'")]
        head = "  ".join(f"{_letters(c)}{r}={s}" for c, s in vals[:4])
        tail = "  ".join(f"{_letters(c)}{r}={s}" for c, s in vals[-2:]) if len(vals) > 6 else ""
        mid = f"  …(+{len(vals) - 6} more)…  " if len(vals) > 6 else "  "
        lines.append(f"r{r}: {label or ''}  {head}{mid}{tail}".rstrip())
    return "\n".join(lines)


def workbook_grids(snapshot: dict, sheet_names: list[str] | None = None, *,
                   title: str, total_cap: int = TOTAL_CHAR_CAP) -> tuple[str, list[str]]:
    """Grids for the named sheets (all data sheets when None), under a total
    budget. Returns (block, notes) — notes name every sheet that was reduced
    or dropped, so smaller context is always visible in the run report."""
    sheets = [s for s in snapshot.get("sheets", [])
              if sheet_names is None or s.get("name") in sheet_names]
    notes: list[str] = []
    parts: list[str] = [f"### {title} ###"]
    used = 0
    for s in sheets:
        g = sheet_grid(s)
        if "[too large" in g.splitlines()[0]:
            notes.append(f"{s.get('name')}: row-summary mode (sheet too large)")
        if used + len(g) > total_cap:
            notes.append(f"{s.get('name')}: omitted (grid budget exhausted)")
            parts.append(f"=== SHEET: {s.get('name')} === [omitted — grid budget exhausted; "
                         "series summaries in the catalogue still cover it]")
            continue
        parts.append(g)
        used += len(g)
    return "\n\n".join(parts), notes
