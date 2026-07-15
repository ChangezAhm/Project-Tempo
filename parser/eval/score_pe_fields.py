"""Live re-score of the PE flash template's FIELD recall after the
configurable-metric-list change to region detection.

Runs the authority region detector (LLM call), maps its slots to (sheet, A1)
cells, derives the data model, ingests the owner's 1/2 marks, and scores value
inputs + new fields. Prints the score and the field cells the detector caught.
"""

from __future__ import annotations

import os

TID = "735b2962-d8c4-4e13-b8ae-600e2b36b957"
BASE = r"C:\Users\chang\Project Tempo\Templates for testing"
NORMAL = os.path.join(BASE, "2026-02-01-pe-portco-flash-template (NORMAL).xlsx")
CLASSIFIED = os.path.join(BASE, "2026-02-01-pe-portco-flash-template (CLASSIFIED).xlsx")


def region_cells_from_payload(regions: list[dict]) -> set[tuple[str, str]]:
    from app.raw_extraction.column_utils import column_letter
    out: set[tuple[str, str]] = set()
    for r in regions:
        sheet = r.get("sheet_name") or ""
        col = int(r.get("label_col") or 0)
        for s in r.get("slots") or []:
            row = int(s.get("row") or 0)
            if col and row:
                out.add((sheet, f"{column_letter(col)}{row}"))
    return out


def main() -> None:
    from app.authoring.regions import detect_and_persist, get_regions
    from app.datamodel.derive import derive_data_model
    from eval.label_ingest import ingest_pair
    from eval.score_labelled import score

    if os.environ.get("REDETECT"):
        print("Running authority region detection (LLM)…")
        reg = detect_and_persist(TID)
    else:
        print("Reading persisted regions…")
        reg = get_regions(TID)
    regions = reg.get("regions") or []
    region_cells = region_cells_from_payload(regions)
    print(f"  regions: {reg.get('count')}  slot-cells: {len(region_cells)}")
    by_kind: dict[str, int] = {}
    for r in regions:
        by_kind[r.get("kind", "?")] = by_kind.get(r.get("kind", "?"), 0) + 1
    print(f"  by kind: {by_kind}")

    print("Deriving data model…")
    dm = derive_data_model(TID)
    facts = [f.model_dump() if hasattr(f, "model_dump") else dict(f) for f in dm.facts]
    for f in facts:
        if "category" in f and hasattr(f["category"], "value"):
            f["category"] = f["category"].value
    print(f"  facts: {len(facts)}")

    print("Ingesting owner marks…")
    labels = ingest_pair(NORMAL, CLASSIFIED)
    print(f"  inputs: {len(labels.inputs)}  fields: {len(labels.fields)}")

    sc = score("PE flash", labels.inputs, labels.fields, facts, region_cells)
    print()
    for line in sc.as_lines():
        print(line)

    # which of the marked fields did we catch, and how?
    gf = {(s, str(c).upper()) for s, c in labels.fields}
    rc = {(s, str(c).upper()) for s, c in region_cells}
    caught = sorted(gf & rc)
    missed = sorted(gf - rc)
    print(f"\nFIELD cells caught by a region slot ({len(caught)}):")
    for s, c in caught:
        print(f"  {s}!{c}")
    print(f"\nFIELD cells still missed ({len(missed)}):")
    for s, c in missed:
        print(f"  {s}!{c}")


if __name__ == "__main__":
    main()
