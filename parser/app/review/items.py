"""Build durable review items from the persisted workbook understanding.

The understanding's review_flags (prose doubts) and its graph-UNSUPPORTED
impact_chains / data_flow claims are turned into rows in
template_review_items (0009). Each item carries a content-hashed item_key
(same question → same key, mirroring datamodel/identity.py's fact_key style)
so re-running the understanding re-asks the same question into the SAME row —
inserts are add-only and human answers survive re-runs.

Kinds:
  judgment          — a prose flag only a human can settle.
  machine_checkable — carries a check_spec the graph verifier
                      (app/review/verify.py) can settle deterministically.
"""

from __future__ import annotations

import hashlib

from app import supabase_client as sb


def make_item(
    *,
    source: str,
    kind: str,
    question: str,
    why: str | None = None,
    affected: dict | None = None,
    suggested_answer: str | None = None,
    check_spec: dict | None = None,
) -> dict:
    """A template_review_items row (sans version id). item_key is content-
    addressed on (source, question) so the same question upserts."""
    item_key = hashlib.sha1(f"{source}|{question}".encode("utf-8")).hexdigest()[:16]
    return {
        "item_key": item_key,
        "source": source,
        "kind": kind,
        "question": question,
        "why": why,
        "affected": affected,
        "suggested_answer": suggested_answer,
        "check_spec": check_spec,
    }


def _sheet_of(target: str) -> str:
    """'Sheet!A1' or \"'My Sheet'!A1\" or bare 'Sheet' → the sheet name."""
    t = str(target).strip()
    i = t.rfind("!")
    return (t[:i] if i != -1 else t).strip().strip("'")


def build_items_from_understanding(workbook: dict) -> list[dict]:
    """PURE: WorkbookUnderstanding-shaped dict → review-item rows.

    - each review_flags string → a judgment item.
    - each impact_chain / data_flow edge with graph_supported is False →
      a machine_checkable item with a check_spec verify.run_check understands.
    - graph_supported None (never checked) or True (confirmed) → no item.
    """
    items: list[dict] = []

    for flag in workbook.get("review_flags") or []:
        items.append(make_item(
            source="understanding",
            kind="judgment",
            question=str(flag),
        ))

    for ch in workbook.get("impact_chains") or []:
        if ch.get("graph_supported") is not False:
            continue
        name = ch.get("name") or "(unnamed)"
        start = ch.get("start") or ""
        flows_to = [str(t) for t in (ch.get("flows_to") or [])]
        items.append(make_item(
            source="understanding",
            kind="machine_checkable",
            question=(
                f"Impact chain '{name}' was NOT confirmed by the dependency graph — "
                f"verify {start} actually flows to {', '.join(flows_to)}."
            ),
            why=ch.get("significance"),
            affected={"sheets": sorted({s for s in map(_sheet_of, flows_to) if s})},
            check_spec={"type": "impact_chain", "start": start, "flows_to": flows_to},
        ))

    for e in workbook.get("data_flow") or []:
        if e.get("graph_supported") is not False:
            continue
        frm = e.get("from_sheet") or ""
        to = e.get("to_sheet") or ""
        what = e.get("what") or "unspecified data"
        items.append(make_item(
            source="understanding",
            kind="machine_checkable",
            question=(
                f"Claimed data flow '{frm}' → '{to}' ({what}) was NOT supported by the "
                f"dependency graph — verify formulas on '{to}' actually read from '{frm}'."
            ),
            affected={"sheets": [s for s in (frm, to) if s]},
            check_spec={"type": "sheet_flow", "from_sheet": frm, "to_sheet": to},
        ))

    return items


def build_and_persist(template_id: str) -> dict:
    """Build review items from the PERSISTED understanding of the latest
    version and insert the new ones (add-only; answers survive re-runs)."""
    version_id, _, _ = sb.get_latest_file(template_id)
    rows = (
        sb.get_client()
        .table("template_understanding")
        .select("understanding, review_flags")
        .eq("template_version_id", version_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return {
            "template_version_id": version_id,
            "added": 0,
            "count": len(sb.list_review_items(version_id)),
            "note": "no persisted understanding for this version — run /understand first",
        }

    persisted = rows[0]
    # upsert_understanding stores the FULL WorkbookUnderstanding dump in the
    # `understanding` jsonb (impact_chains + data_flow included), plus lifted
    # columns like review_flags. Prefer the full dump; fall back to the lifted
    # review_flags column for legacy/partial rows.
    workbook = dict(persisted.get("understanding") or {})
    note = None
    if not workbook.get("review_flags"):
        workbook["review_flags"] = persisted.get("review_flags") or []
    if "impact_chains" not in workbook and "data_flow" not in workbook:
        note = ("persisted understanding has no impact_chains/data_flow — "
                "items built from review_flags only")

    items = build_items_from_understanding(workbook)
    added = sb.insert_review_items(version_id, items)
    out = {
        "template_version_id": version_id,
        "added": added,
        "count": len(sb.list_review_items(version_id)),
    }
    if note:
        out["note"] = note
    return out
