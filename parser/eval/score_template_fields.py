"""Live field/input scoring for a hand-labelled template.

Usage:
  python -m eval.score_template_fields <template_id> <NORMAL.xlsx> <CLASSIFIED.xlsx> [--redetect]

Materialises the data model, runs the authority region detector (LLM) when
--redetect is given (else reads persisted regions), ingests the owner's 1/2
marks, and scores value inputs + new fields. Prints the score, the region kinds,
and which marked fields were caught vs missed.
"""

from __future__ import annotations

import sys


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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    redetect = "--redetect" in sys.argv[1:]
    tid, normal, classified = args[0], args[1], args[2]

    from app.authoring.regions import detect_and_persist, get_regions
    from app.datamodel.persist import derive_and_persist, get_data_model
    from eval.label_ingest import ingest_pair
    from eval.score_labelled import score

    print("Materialising data model…")
    dm = get_data_model(tid, limit=30000)
    if not dm.get("available"):
        derive_and_persist(tid)
        dm = get_data_model(tid, limit=30000)
    facts = dm.get("facts") or []
    print(f"  facts: {len(facts)}")

    if redetect:
        print("Running authority region detection (LLM)…")
        reg = detect_and_persist(tid)
    else:
        print("Reading persisted regions…")
        reg = get_regions(tid)
    regions = reg.get("regions") or []
    region_cells = region_cells_from_payload(regions)
    by_kind: dict[str, int] = {}
    for r in regions:
        by_kind[r.get("kind", "?")] = by_kind.get(r.get("kind", "?"), 0) + 1
    print(f"  regions: {reg.get('count')}  slot-cells: {len(region_cells)}  by kind: {by_kind}")

    print("Ingesting owner marks…")
    labels = ingest_pair(normal, classified)
    print(f"  inputs: {len(labels.inputs)}  fields: {len(labels.fields)}")

    sc = score(tid, labels.inputs, labels.fields, facts, region_cells)
    print()
    for line in sc.as_lines():
        print(line)

    gf = {(s, str(c).upper()) for s, c in labels.fields}
    rc = {(s, str(c).upper()) for s, c in region_cells}
    caught, missed = sorted(gf & rc), sorted(gf - rc)
    over = sorted(rc - gf)   # region slots on cells the owner did NOT mark as fields
    print(f"\nFIELD cells caught ({len(caught)}):")
    for s, c in caught:
        print(f"  {s}!{c}")
    print(f"\nFIELD cells missed ({len(missed)}):")
    for s, c in missed:
        print(f"  {s}!{c}")
    print(f"\nRegion slot-cells NOT owner-marked fields ({len(over)}) — blank add-slots or over-detection:")
    for s, c in over:
        print(f"  {s}!{c}")


if __name__ == "__main__":
    main()
