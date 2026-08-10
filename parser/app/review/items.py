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


# ---------------------------------------------------------------------------
# THE single gate for filing questions (owner rule: FEW, BINARY, CURRENT).
# Per-family caps keep the whole inbox at <= ~15 open questions per template;
# every question must be one-tap answerable; a new run SUPERSEDES the previous
# run's unanswered questions instead of piling on top of them.
MAX_OPEN_TOTAL = 15
_FAMILY_CAPS = {"populate": 10, "onboarding-regions": 3, "understanding": 2}


def file_questions(version_id: str, items: list[dict], *, family: str,
                   cap: int | None = None) -> dict:
    """File this run's questions through the budgeted gate.

    - dedupes by item_key; DROPS any item without a suggested_answer (a
      question the user can't one-tap is a defect, not a question)
    - supersedes still-open questions of the same family that this run did
      not re-ask (stale phrasings never accumulate across runs)
    - answered questions are never re-asked (insert skips existing keys)
    - enforces the family cap, keeping highest-priority items (sort key:
      transient "_priority", lower = more important, default 5)
    """
    cap = cap if cap is not None else _FAMILY_CAPS.get(family, 3)
    seen: set = set()
    deduped: list[dict] = []
    for it in items:
        k = it.get("item_key")
        if k and k not in seen:
            seen.add(k)
            deduped.append(it)
    binary = [it for it in deduped if it.get("suggested_answer")]
    binary.sort(key=lambda it: it.get("_priority", 5))
    for it in binary:
        it.pop("_priority", None)

    existing = sb.list_review_items(version_id)
    open_family = [e for e in existing if e.get("status") == "open"
                   and (e.get("source") or "").startswith(family)]
    existing_keys = {e.get("item_key") for e in existing}
    new_keys = {it["item_key"] for it in binary}

    superseded = 0
    for e in open_family:
        if e.get("item_key") not in new_keys:
            try:
                sb.update_review_item(e["id"], {"status": "superseded"})
                superseded += 1
            except Exception:  # noqa: BLE001 — best effort; a stale extra never blocks
                pass

    kept_open = sum(1 for e in open_family if e.get("item_key") in new_keys)
    budget = max(0, cap - kept_open)
    to_file = []
    for it in binary:
        if it["item_key"] in existing_keys:
            continue
        if len(to_file) >= budget:
            break
        to_file.append(it)
    filed = sb.insert_review_items(version_id, to_file) if to_file else 0
    return {"filed": filed, "superseded": superseded, "kept_open": kept_open,
            "dropped_over_cap": max(0, len(binary) - len(to_file)
                                    - sum(1 for it in binary if it["item_key"] in existing_keys)),
            "dropped_nonbinary": len(deduped) - len(binary)}


def _sheet_of(target: str) -> str:
    """'Sheet!A1' or \"'My Sheet'!A1\" or bare 'Sheet' → the sheet name."""
    t = str(target).strip()
    i = t.rfind("!")
    return (t[:i] if i != -1 else t).strip().strip("'")


def _human(target: str) -> str:
    """A cell reference as a PERSON would name it — the business label, never the
    address. 'Monthly Flash!B20 (Net Debt)' -> 'Net Debt'; 'P&L!B15' -> 'the P&L
    figure'. The owner built the template by meaning, not by coordinate, so the
    question must speak in meaning."""
    t = str(target).strip()
    a, b = t.rfind("("), t.rfind(")")
    if a != -1 and b > a:
        label = t[a + 1:b].strip()
        if label:
            return label
    sheet = _sheet_of(t)
    return f"the {sheet} figure" if sheet else t


def _human_list(targets: list[str]) -> str:
    labels = [_human(t) for t in targets if str(t).strip()]
    if not labels:
        return "these figures"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


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
        start = ch.get("start") or ""
        flows_to = [str(t) for t in (ch.get("flows_to") or [])]
        # The LLM expected an input to drive some outputs, but no formula confirms
        # it. Don't make the owner referee that — ask the ONE thing it decides for
        # populating: are those outputs calculated by the template, or entered?
        outs = _human_list(flows_to)
        items.append(make_item(
            source="understanding",
            kind="machine_checkable",
            question=(
                f"Does the template work out {outs} on its own, or is that a figure someone "
                f"types in (or brings in from another report)?"
            ),
            why=(f"I can see {_human(start)}, plus {outs}, in the template but couldn't find a "
                 "formula linking them, so I'm not sure whether to fill those cells from your "
                 "source file or leave them for the template to calculate."),
            affected={"sheets": sorted({s for s in map(_sheet_of, flows_to) if s})},
            check_spec={"type": "impact_chain", "start": start, "flows_to": flows_to},
        ))

    for e in workbook.get("data_flow") or []:
        if e.get("graph_supported") is not False:
            continue
        frm = e.get("from_sheet") or ""
        to = e.get("to_sheet") or ""
        what = e.get("what") or "these figures"
        items.append(make_item(
            source="understanding",
            kind="machine_checkable",
            question=(
                f"On '{to}', are the {what} figures pulled from '{frm}', or entered separately "
                f"on '{to}'?"
            ),
            why=(f"I expected {what} to carry over from '{frm}' into '{to}' but the formulas "
                 "don't show that link — this tells me whether to fill those cells or leave "
                 "them linked to the other sheet."),
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
    for it in items:
        if not it.get("suggested_answer"):
            it["suggested_answer"] = "yes — this reading is right"
    stats = file_questions(version_id, items, family="understanding")
    added = stats["filed"]
    out = {
        "template_version_id": version_id,
        "added": added,
        "count": len(sb.list_review_items(version_id)),
    }
    if note:
        out["note"] = note
    return out
