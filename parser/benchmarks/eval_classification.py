"""Ground-truth eval: the system's cell classification vs the owner's 1/2 markings.

The CLASSIFIED workbooks encode ground truth by REPLACING each input cell's
value with its marking: 1 = fixed input, 2 = configurable/custom (incl.
editable labels, validation-driven cells). A cell is labeled iff its CLASSIFIED
value is 1/2 and differs from the NORMAL twin (so real 1s and 2s in the data
don't read as labels).

Scoring (per file and overall):
  found_input   marked cell whose fact category is data/sourced (the system will
                populate it)
  found_config  marked cell classified config/staging (the system sees it, but
                as a control/placeholder — right answer for many 2-cells,
                a miss for 1-cells)
  computed      marked cell the system considers formula-computed
  absent        marked cell with NO fact at all (invisible to the system)
  false_inputs  UNMARKED cells the system would populate (data/sourced) —
                the inverse error

Usage (from parser/):  python benchmarks/eval_classification.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aspose.cells import Workbook  # noqa: E402

BASE = Path("C:/Users/chang/Project Tempo/Templates for testing")

CASES = [
    # flash removed from the workspace 2026-08-06 — re-add after re-upload
    ("2026-07-13-saas-financial-model", "a05cf69d-38c8-456b-82b9-b158d6fa97b4"),
    ("Flash Collection_MasterTemplate_2.1.6_PROD", "a8c073b1-ecac-4e2c-b456-e1a45c92d6a7"),
]


def a1(row0: int, col0: int) -> str:
    letters = ""
    c = col0 + 1
    while c > 0:
        c, rem = divmod(c - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row0 + 1}"


def ground_truth(stem: str) -> dict[tuple[str, str], int]:
    """{(sheet, A1) -> 1|2} from the CLASSIFIED/NORMAL pair."""
    wc = Workbook(str(BASE / f"{stem} (CLASSIFIED).xlsx"))
    wn = Workbook(str(BASE / f"{stem} (NORMAL).xlsx"))
    normal = {}
    for s in wn.worksheets:
        for r in range(s.cells.max_data_row + 1):
            for c in range(s.cells.max_data_column + 1):
                v = s.cells.get(r, c).value
                if v is not None:
                    normal[(s.name, r, c)] = v
    labels: dict[tuple[str, str], int] = {}
    for s in wc.worksheets:
        for r in range(s.cells.max_data_row + 1):
            for c in range(s.cells.max_data_column + 1):
                v = s.cells.get(r, c).value
                if v in (1, 2, 1.0, 2.0):
                    nv = normal.get((s.name, r, c))
                    if nv is None or str(nv) != str(int(v)):
                        labels[(s.name, a1(r, c))] = int(v)
    return labels


def system_view(template_id: str) -> tuple[dict[tuple[str, str], dict], list[dict]]:
    """({(sheet, A1) -> fact}, extensible_regions) — derives the model if missing."""
    from app import supabase_client as sb
    from app.datamodel.persist import derive_and_persist, get_data_model

    dm = get_data_model(template_id, limit=30000)
    from app.datamodel.derive import DERIVATION_VERSION
    ver = ((dm.get("model") or {}).get("dimensions") or {}).get("derivation_version", 0)
    if not dm.get("available") or ver < DERIVATION_VERSION:
        print(f"  deriving data model (stored v{ver} < v{DERIVATION_VERSION})…")
        derive_and_persist(template_id)
        dm = get_data_model(template_id, limit=30000)
    regions: list[dict] = []
    try:
        vid, _p, _f = sb.get_latest_file(template_id)
        regions = sb.list_extensible_regions(vid) or []
    except Exception as e:  # noqa: BLE001
        print(f"  (regions unavailable: {e})")
    return {(f["sheet_name"], (f.get("cell") or "").upper()): f for f in dm["facts"]}, regions


def in_region(sheet: str, cell: str, regions: list[dict]) -> bool:
    import re
    m = re.match(r"([A-Z]+)(\d+)", cell)
    if not m:
        return False
    row = int(m.group(2))
    for rg in regions:
        if rg.get("sheet_name") != sheet:
            continue
        lo = rg.get("row_start") or 0
        hi = rg.get("row_end") or 0
        slot_rows = {s.get("row") for s in (rg.get("slots") or []) if isinstance(s, dict)}
        if (lo and hi and lo <= row <= hi) or row in slot_rows:
            return True
    return False


def main() -> None:
    grand = Counter()
    for stem, tid in CASES:
        labels = ground_truth(stem)
        facts, regions = system_view(tid)
        print(f"\n{'=' * 70}\n{stem}\n  marked cells: {len(labels)} "
              f"(1s: {sum(1 for v in labels.values() if v == 1)}, "
              f"2s: {sum(1 for v in labels.values() if v == 2)}) | system facts: {len(facts)} "
              f"| extensible regions: {len(regions)}")

        tally = Counter()
        misses: list[str] = []
        for (sheet, cell), mark in sorted(labels.items()):
            f = facts.get((sheet, cell))
            cat = (f or {}).get("category")
            if f is None:
                # a configurable/custom cell recognized as an extensible-region
                # slot is a HIT for mark 2 — the additions path owns those cells
                bucket = "region_slot" if in_region(sheet, cell, regions) else "absent"
            elif cat in ("data", "sourced"):
                bucket = "found_input"
            elif cat in ("config", "staging"):
                bucket = "region_slot" if in_region(sheet, cell, regions) else "found_config"
            elif cat == "computed":
                bucket = "computed"
            else:
                bucket = f"other:{cat}"
            tally[(mark, bucket)] += 1
            grand[(mark, bucket)] += 1
            if bucket in ("absent", "computed") or (mark == 1 and bucket not in ("found_input",)):
                if len(misses) < 30:
                    label = (f or {}).get("metric_label") or ""
                    misses.append(f"    [{mark}] {sheet}!{cell} -> {bucket} {label[:40]!r}")

        for mark in (1, 2):
            total = sum(n for (m, _b), n in tally.items() if m == mark)
            if not total:
                continue
            print(f"  mark {mark} ({total} cells):")
            for (m, b), n in sorted(tally.items(), key=lambda kv: -kv[1]):
                if m == mark:
                    print(f"      {b:14s} {n:5d}  ({n / total:5.1%})")
        # headline: 1-cells the system will populate
        ones = sum(n for (m, _b), n in tally.items() if m == 1)
        hit = tally.get((1, "found_input"), 0)
        if ones:
            print(f"  HEADLINE mark-1 accuracy (system will populate): {hit}/{ones} = {hit / ones:.1%}")
        if misses:
            print("  disagreements (first 30):")
            print("\n".join(misses))

        # inverse error: unmarked cells the system would populate
        false_inputs = [(s, c, f) for (s, c), f in facts.items()
                        if f.get("category") in ("data", "sourced") and (s, c) not in labels]
        print(f"  false inputs (system fills, you didn't mark): {len(false_inputs)}")
        for s, c, f in false_inputs[:15]:
            print(f"      {s}!{c}  {(f.get('metric_label') or '')[:44]!r}")

    print(f"\n{'=' * 70}\nOVERALL")
    for mark in (1, 2):
        total = sum(n for (m, _b), n in grand.items() if m == mark)
        if not total:
            continue
        print(f"  mark {mark} ({total} cells):")
        for (m, b), n in sorted(grand.items(), key=lambda kv: -kv[1]):
            if m == mark:
                print(f"      {b:14s} {n:5d}  ({n / total:5.1%})")


if __name__ == "__main__":
    main()
