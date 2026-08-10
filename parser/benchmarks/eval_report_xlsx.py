"""Visual eval report: colour every marked cell in a COPY of the CLASSIFIED
workbook by what the system did with it, so disagreements can be eyeballed in
Excel. The original CLASSIFIED files are never touched — output is written as
"<stem> (CLASSIFIED) - EVAL.xlsx" with an EVAL LEGEND sheet at the front.

Colours:
  GREEN   agreement — the system treats this marked cell as a fillable input
  RED     MISSED — the system has no idea this cell exists (no fact, no region)
  ORANGE  marked, but the cell holds a FORMULA in the NORMAL file — the system
          deliberately never writes formulas (definitional disagreement)
  BLUE    recognized as an extensible-region slot (filled via the add-line
          path, not as a plain data cell)
  PURPLE  seen but classified config/staging (control/placeholder)
  YELLOW  the INVERSE error: a cell you did NOT mark that the system WOULD
          fill (skipped where marking/placeholder values collide and the
          ground truth is ambiguous)

Usage (from parser/):  python benchmarks/eval_report_xlsx.py
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aspose.cells import BackgroundType, Workbook  # noqa: E402
from aspose.pydrawing import Color  # noqa: E402

from eval_classification import BASE, CASES, a1, ground_truth, in_region, system_view  # noqa: E402

COLORS = {
    "agree": ("GREEN", Color.from_argb(0xFF, 0x63, 0xBE, 0x7B),
              "agreement — system fills this marked cell"),
    "missed": ("RED", Color.from_argb(0xFF, 0xFF, 0x00, 0x00),
               "MISSED — invisible to the system (no fact, no region)"),
    "formula": ("ORANGE", Color.from_argb(0xFF, 0xFF, 0xA5, 0x00),
                "marked but holds a FORMULA — system never writes formulas"),
    "mirror": ("CYAN", Color.from_argb(0xFF, 0x9E, 0xE5, 0xE5),
               "pure display mirror (=X of another cell) — the write target is the "
               "chain front; refusing to write here is CORRECT"),
    "region": ("BLUE", Color.from_argb(0xFF, 0x9D, 0xC3, 0xE6),
               "recognized as extensible-region slot (add-line path)"),
    "config": ("PURPLE", Color.from_argb(0xFF, 0xC9, 0xA0, 0xDC),
               "seen but classified config/staging (control/placeholder)"),
    "false_input": ("YELLOW", Color.from_argb(0xFF, 0xFF, 0xF2, 0x00),
                    "NOT marked by you, but the system would fill it"),
    "amb_filled": ("PALE GREEN", Color.from_argb(0xFF, 0xD8, 0xE4, 0xBC),
                   "system FILLS it — your marking is indistinguishable from the "
                   "template's own value (collision), so unverifiable"),
    "amb_unknown": ("PALE PINK", Color.from_argb(0xFF, 0xF2, 0xC4, 0xC4),
                    "system does NOT fill it — marking/value collision, unverifiable"),
}


def normal_formulas(stem: str) -> dict[tuple[str, str], str]:
    wb = Workbook(str(BASE / f"{stem} (NORMAL).xlsx"))
    out: dict[tuple[str, str], str] = {}
    for s in wb.worksheets:
        for r in range(s.cells.max_data_row + 1):
            for c in range(s.cells.max_data_column + 1):
                f = s.cells.get(r, c).formula
                if f:
                    out[(s.name, a1(r, c))] = f
    return out


def ambiguous_cells(stem: str) -> set[tuple[str, str]]:
    """Cells where CLASSIFIED == NORMAL ∈ {1,2}: marking indistinguishable from a
    placeholder value — excluded from the false-input sweep."""
    wc = Workbook(str(BASE / f"{stem} (CLASSIFIED).xlsx"))
    wn = Workbook(str(BASE / f"{stem} (NORMAL).xlsx"))
    normal = {}
    for s in wn.worksheets:
        for r in range(s.cells.max_data_row + 1):
            for c in range(s.cells.max_data_column + 1):
                v = s.cells.get(r, c).value
                if v is not None:
                    normal[(s.name, r, c)] = v
    out: set[tuple[str, str]] = set()
    for s in wc.worksheets:
        for r in range(s.cells.max_data_row + 1):
            for c in range(s.cells.max_data_column + 1):
                v = s.cells.get(r, c).value
                if v in (1, 2, 1.0, 2.0) and str(normal.get((s.name, r, c))) == str(int(v)):
                    out.add((s.name, a1(r, c)))
    return out


def bucket_for(mark_cell, fact, formula, in_rgn) -> str:
    from eval_classification import MIRROR_RE
    cat = (fact or {}).get("category")
    if cat in ("data", "sourced"):
        return "agree"
    if formula and MIRROR_RE.match(formula):
        return "mirror"
    if formula:
        return "formula"
    if in_rgn:
        return "region"
    if fact is not None and cat in ("config", "staging"):
        return "config"
    return "missed"


def paint(ws, row0: int, col0: int, color) -> None:
    cell = ws.cells.get(row0, col0)
    style = cell.get_style()
    style.foreground_color = color
    style.pattern = BackgroundType.SOLID
    cell.set_style(style)


def build_report(stem: str, template_id: str) -> None:
    labels = ground_truth(stem)
    facts, regions = system_view(template_id)
    formulas = normal_formulas(stem)
    ambiguous = ambiguous_cells(stem)

    wb = Workbook(str(BASE / f"{stem} (CLASSIFIED).xlsx"))
    sheets = {s.name: s for s in wb.worksheets}
    counts: Counter = Counter()

    for (sheet, cell), mark in labels.items():
        ws = sheets.get(sheet)
        m = re.match(r"([A-Z]+)(\d+)", cell)
        if ws is None or not m:
            continue
        col0 = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(m.group(1)))) - 1
        row0 = int(m.group(2)) - 1
        b = bucket_for(mark, facts.get((sheet, cell)), formulas.get((sheet, cell)),
                       in_region(sheet, cell, regions))
        counts[b] += 1
        paint(ws, row0, col0, COLORS[b][1])

    # ambiguity made visible: collision cells (CLASSIFIED == NORMAL == 1/2) can't
    # prove a marking, but the system's own verdict on them CAN be shown — this
    # is the flash P&L's whole top input block
    for (sheet, cell) in ambiguous:
        ws = sheets.get(sheet)
        m = re.match(r"([A-Z]+)(\d+)", cell)
        if ws is None or not m:
            continue
        col0 = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(m.group(1)))) - 1
        row0 = int(m.group(2)) - 1
        f = facts.get((sheet, cell))
        b = "amb_filled" if f is not None and f.get("category") in ("data", "sourced") else "amb_unknown"
        counts[b] += 1
        paint(ws, row0, col0, COLORS[b][1])

    # inverse error: unmarked cells the system would fill (ambiguous cells skipped)
    for (sheet, cell), f in facts.items():
        if f.get("category") not in ("data", "sourced"):
            continue
        if (sheet, cell) in labels or (sheet, cell) in ambiguous:
            continue
        ws = sheets.get(sheet)
        m = re.match(r"([A-Z]+)(\d+)", cell or "")
        if ws is None or not m:
            continue
        col0 = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(m.group(1)))) - 1
        row0 = int(m.group(2)) - 1
        counts["false_input"] += 1
        paint(ws, row0, col0, COLORS["false_input"][1])

    # legend sheet at the front
    lg = wb.worksheets.add("EVAL LEGEND")
    if not hasattr(lg, "cells"):          # older API returns the index instead
        lg = wb.worksheets[int(lg)]
    lg.move_to(0)
    lg.cells.get(0, 1).put_value(f"Classification eval — {stem}")
    lg.cells.get(1, 1).put_value("Every cell you marked (1/2) is coloured by what the system did with it:")
    r = 3
    for key, (name, color, desc) in COLORS.items():
        paint(lg, r, 1, color)
        lg.cells.get(r, 1).put_value(name)
        lg.cells.get(r, 2).put_value(desc)
        lg.cells.get(r, 4).put_value(int(counts.get(key, 0)))
        r += 1
    lg.cells.get(r + 1, 1).put_value(
        "Note: YELLOW skips cells whose marking collides with a placeholder value "
        "(CLASSIFIED == NORMAL == 1/2) — ground truth is ambiguous there.")
    lg.cells.get(r + 2, 1).put_value(f"Marked cells scored: {len(labels)}")
    for c, w in ((1, 14.0), (2, 70.0), (4, 8.0)):
        lg.cells.set_column_width(c, w)

    out = BASE / f"{stem} (CLASSIFIED) - EVAL.xlsx"
    try:
        wb.save(str(out))
    except RuntimeError:
        # the file is open in Excel — write a numbered sibling instead of dying
        for n in range(2, 10):
            alt = BASE / f"{stem} (CLASSIFIED) - EVAL ({n}).xlsx"
            try:
                wb.save(str(alt))
                out = alt
                break
            except RuntimeError:
                continue
    print(f"{stem}: {dict(counts)} -> {out.name}")


if __name__ == "__main__":
    for stem, tid in CASES:
        try:
            build_report(stem, tid)
        except Exception as e:  # noqa: BLE001 — one template must not sink the others
            print(f"{stem}: FAILED — {e}")
