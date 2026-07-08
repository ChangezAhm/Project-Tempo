"""Deterministic verifier for machine-checkable review items ("Verify for me").

run_check re-tests a check_spec against the parsed snapshot: sheet_flow
recomputes the real cross-sheet edges from cell precedents; impact_chain
re-traces the forward dependency closure via pipeline.build_dependents_index +
trace_impact. No LLM anywhere — the verdict is graph fact, and the evidence
is written into the item's resolution so the claim stays auditable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app import supabase_client as sb
from app.pipeline import build_dependents_index, trace_impact
from app.raw_extraction.formula_mapper import _range_bounds, _split_qualified

_MAX_SAMPLE_REFS = 5
_MAX_SAMPLE_CELLS = 3
_TRACE_DEPTH = 6        # generous — LLM chains skip intermediate hops
_TRACE_MAX_TOTAL = 500


def _check_sheet_flow(spec: dict, snap: dict) -> tuple[str, dict]:
    """Does any formula on to_sheet read from from_sheet? Recomputes the
    cross-sheet edges from cells[].precedents (same signal as the
    understanding's verifier, reimplemented locally on purpose)."""
    frm = str(spec.get("from_sheet") or "").strip().strip("'")
    to = str(spec.get("to_sheet") or "").strip().strip("'")
    edge_count = 0
    searched = 0
    samples: list[str] = []
    for s in snap.get("sheets", []):
        name = s["name"]
        for c in s.get("cells", []):
            for rng in c.get("precedents", []) or []:
                i = rng.rfind("!")
                if i == -1:
                    continue
                ref = rng[:i].strip("'")
                if ref == name:
                    continue
                searched += 1
                if ref == frm and name == to:
                    edge_count += 1
                    if len(samples) < _MAX_SAMPLE_REFS:
                        samples.append(f"{name}!{c.get('address')} <- {rng}")
    if edge_count:
        return "verified", {"edge_count": edge_count, "sample_refs": samples}
    return "refuted", {"searched_edges": searched}


def _check_impact_chain(spec: dict, snap: dict) -> tuple[str, dict]:
    """Does start's forward dependency closure actually reach every flows_to
    target? Targets may be 'Sheet!Cell' (exact) or a bare sheet name (any
    closure cell on that sheet counts)."""
    start = str(spec.get("start") or "").strip()
    sheet, addr = _split_qualified(start)
    sheet = sheet.strip().strip("'")
    addr = addr.strip().upper()
    if not sheet or not _range_bounds(addr):
        return "inconclusive", {"reason": "start is not a cell reference"}

    targets = [str(t).strip() for t in (spec.get("flows_to") or []) if str(t).strip()]
    if not targets:
        return "inconclusive", {"reason": "no flows_to targets"}

    index = build_dependents_index(snap)
    res = trace_impact(index, set(), {}, f"{sheet}!{addr}",
                       depth=_TRACE_DEPTH, max_total=_TRACE_MAX_TOTAL)
    affected = res["affected"]
    closure_cells = {a["cell"] for a in affected}
    by_sheet: dict[str, list[str]] = {}
    for a in affected:
        if a["sheet"]:
            by_sheet.setdefault(a["sheet"].strip("'"), []).append(a["cell"])

    reached: dict[str, list[str]] = {}
    not_reached: list[str] = []
    for t in targets:
        tsheet, taddr = _split_qualified(t)
        tsheet = tsheet.strip().strip("'")
        if tsheet:  # 'Sheet!Cell' — exact cell must be in the closure
            q = f"{tsheet}!{taddr.strip().upper()}"
            if q in closure_cells:
                reached[t] = [q]
            else:
                not_reached.append(t)
        else:  # bare sheet name — any closure cell on that sheet
            cells = by_sheet.get(taddr.strip().strip("'"))
            if cells:
                reached[t] = cells[:_MAX_SAMPLE_CELLS]
            else:
                not_reached.append(t)

    evidence = {"reached": reached, "not_reached": not_reached,
                "closure_size": len(affected)}
    if not not_reached:
        return "verified", evidence
    if not reached:
        return "refuted", evidence
    return "inconclusive", evidence


def run_check(check_spec: dict, snap: dict) -> tuple[str, dict]:
    """(status, evidence) where status ∈ verified | refuted | inconclusive."""
    kind = (check_spec or {}).get("type")
    if kind == "sheet_flow":
        return _check_sheet_flow(check_spec, snap)
    if kind == "impact_chain":
        return _check_impact_chain(check_spec, snap)
    return "inconclusive", {"reason": "no verifier for this check type"}


def verify_item(template_id: str, item_id: str) -> dict:
    """Run an item's check against the snapshot and record the verdict.

    Missing / non-machine-checkable / spec-less items change nothing and
    return an {"error": ...} dict (404-style for the caller). Inconclusive
    checks keep status 'open' (the documented status vocabulary has no
    'inconclusive') but still record the attempt in resolution."""
    item = sb.get_review_item(item_id)
    if item is None:
        return {"error": f"review item {item_id} not found"}
    if item.get("kind") != "machine_checkable":
        return {"error": "item is not machine_checkable"}
    spec = item.get("check_spec")
    if not spec:
        return {"error": "item has no check_spec"}

    # Lazy import: derive pulls in the Aspose re-parse fallback — heavy, and
    # only needed when a check actually runs.
    from app.datamodel.derive import _load_snapshot

    snap = _load_snapshot(item["template_version_id"], template_id)
    status, evidence = run_check(spec, snap)
    new_status = status if status in ("verified", "refuted") else item.get("status", "open")
    return sb.update_review_item(item_id, {
        "status": new_status,
        "resolution": {
            "evidence": evidence,
            "outcome": status,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolved_by": "graph-verifier",
        },
    })
