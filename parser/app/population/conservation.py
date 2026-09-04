"""Conservation of source data — no child of a consumed family goes missing.

Two deterministic facts from the SOURCE's own formula graph power two guards:

1. FAMILY GAPS (the missing-SBP class): when the mapper consumes SOME children
   of a source total (r28 'Total EBITDA add-backs' = restructuring + legal +
   CMO + share-based payments; three of the four mapped into template buckets),
   the remaining children are ORPHANS — real amounts that silently vanish from
   the fill. `family_gaps` finds them; `place_orphans` asks the model ONE
   focused question per run (meaning: which bucket does each orphan belong to,
   per the template's own definitions — or why it's excluded); unplaced orphans
   become a review question. Code detects, AI assigns meaning, verify re-checks.

2. REDUNDANT SERIES (the additions-dupes class): a source row whose value the
   graph derives from already-consumed rows (Gross profit = used revenue lines
   − used COGS; every margin over them), or a child of a consumed TOTAL (staff
   costs inside a consumed 'Total operating expenses'), carries no NEW
   information — proposing it as a custom line only duplicates what's already
   filled. `redundant_series` returns them so additions rank true novelty
   (the KPI block) instead.

Detection is pure snapshot analysis (reuses aggregation.py's ref parsing);
the single LLM step is spend-guarded like every other call.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict

from app.population.aggregation import _index, _parse_ref, total_leaf_rows

logger = logging.getLogger(__name__)

_SID = re.compile(r"^(.*)!r(\d+)$")


def _sid_rows(sids) -> set[tuple[str, int]]:
    out = set()
    for sid in sids or []:
        m = _SID.match(sid or "")
        if m:
            out.add((m.group(1), int(m.group(2))))
    return out


def _row_precedents(snapshot: dict) -> dict[tuple[str, int], set[tuple[str, int]]]:
    """Row-level formula graph: {(sheet,row) -> precedent rows} for rows that
    hold ANY formula (not just additive ones — a margin/ratio derives too)."""
    by_rc, _ = _index(snapshot)
    out: dict[tuple[str, int], set[tuple[str, int]]] = defaultdict(set)
    for sheet, cells in by_rc.items():
        for (row, col), c in cells.items():
            if not c.get("formula"):
                continue
            for pr in c.get("precedents") or []:
                parsed = _parse_ref(pr, sheet)
                if not parsed:
                    continue
                psheet, r1, _c1, r2, _c2 = parsed
                if r2 - r1 > 200:          # a whole-column ref isn't a row lineage
                    continue
                for r in range(r1, r2 + 1):
                    if (psheet, r) != (sheet, row):
                        out[(sheet, row)].add((psheet, r))
    return dict(out)


def _label_tokens(label: str | None) -> frozenset[str]:
    """Order-free identity of a display label ('EBITDA (reported)' ==
    'Reported EBITDA'), for the display-dupe check."""
    return frozenset(t for t in re.split(r"[^a-z0-9]+", (label or "").lower()) if t)


def redundant_series(snapshot: dict, catalogue: dict, used_sids: set[str],
                     display_labels: set[frozenset] | None = None) -> set[str]:
    """Series that would only DUPLICATE what the fill already shows:

    (a) derivable from used rows AND label-matching a line the template already
        displays (``display_labels``: token-sets of used series labels + demand
        labels). Derivability ALONE is not redundancy — LTM revenue, DSO,
        leverage are all computable from consumed rows, but the ratio AS A LINE
        is exactly what a KPI region exists to show (an earlier rule equated
        'derivable' with 'worthless' and excluded 8 of a source's 19 KPIs).
        With no ``display_labels`` given, any derivable row is redundant (the
        stricter legacy behaviour).
    (b) children of a USED aggregate: their amounts sit inside a consumed
        total — re-adding them as lines double-displays the same money.

    Deterministic; empty when the source has no formulas."""
    used_rows = _sid_rows(used_sids)
    if not used_rows:
        return set()
    prec = _row_precedents(snapshot)

    # derived-from-used, to a fixpoint (margin over a derived subtotal etc.)
    derivable: set[tuple[str, int]] = set()
    changed = True
    while changed:
        changed = False
        for rr, ps in prec.items():
            if rr in derivable or rr in used_rows or not ps:
                continue
            if ps <= (used_rows | derivable):
                derivable.add(rr)
                changed = True

    redundant: set[tuple[str, int]] = set()
    label_by_row = {(s.sheet, s.row): s.label for s in catalogue.values()}
    for rr in derivable:
        if display_labels is None:
            redundant.add(rr)
        elif _label_tokens(label_by_row.get(rr)) in display_labels:
            redundant.add(rr)          # a display dupe of an already-shown line

    # children of a USED aggregate
    for total, children in total_leaf_rows(snapshot).items():
        if total in used_rows:
            redundant |= {c for c in children if c not in used_rows}

    return {sid for sid, s in catalogue.items()
            if (s.sheet, s.row) in redundant}


def family_gaps(snapshot: dict, catalogue: dict, metric_maps) -> list[dict]:
    """Partially-consumed families. A FAMILY is a formula row and its DIRECT
    row-precedents — the statement's own structure, one level deep. (Transitive
    leaf expansion once pierced THROUGH a consumed subtotal and exposed its own
    components as 'orphans' of a higher total — the Capex double-count.)

    A gap = a family with ≥1 member consumed and ≥1 direct child that is in the
    catalogue but unused. Emitted whether or not the TOTAL itself is consumed:
    when it is (``total_used``), the orphan usually belongs on an UNFILLED
    template component line (the template displays components AND their total,
    so mirroring both is correct, not double-counting — arithmetic reuse inside
    one cell's SUM is what the placement guard forbids)."""
    from app.population.region_bridge import used_series
    used = used_series(metric_maps)
    used_rows = _sid_rows(used)
    if not used_rows:
        return []
    metric_by_sid: dict[str, str] = {}
    for m in metric_maps:
        for sid in [m.series_id, *(m.also_series_ids or [])]:
            if sid:
                metric_by_sid.setdefault(sid, m.metric)

    prec = _row_precedents(snapshot)
    gaps: list[dict] = []
    for (sheet, trow), children in prec.items():
        kids = sorted(r for (sh, r) in children if sh == sheet)
        if len(kids) != len(children) or not (2 <= len(kids) <= 12):
            continue                        # cross-sheet / degenerate: out of scope
        total_used = (sheet, trow) in used_rows
        consumed = [r for r in kids if (sheet, r) in used_rows]
        orphans = [r for r in kids
                   if (sheet, r) not in used_rows and f"{sheet}!r{r}" in catalogue]
        if not (consumed or total_used) or not orphans:
            continue
        total_sid = f"{sheet}!r{trow}"
        gaps.append({
            "sheet": sheet, "total_row": trow,
            "total_label": catalogue[total_sid].label if total_sid in catalogue else f"row {trow}",
            "total_used": total_used,
            "total_metric": metric_by_sid.get(total_sid),
            "consumed": [{"sid": f"{sheet}!r{r}",
                          "label": catalogue.get(f"{sheet}!r{r}").label if f"{sheet}!r{r}" in catalogue else f"row {r}",
                          "metric": metric_by_sid.get(f"{sheet}!r{r}")}
                         for r in consumed],
            "orphans": [{"sid": f"{sheet}!r{r}", "label": catalogue[f"{sheet}!r{r}"].label}
                        for r in orphans],
        })
    return gaps


_PLACE_SYSTEM = (
    "A SOURCE workbook total partitions into component rows. The template consumed some "
    "of the family (components and/or the total); the remaining components are ORPHANS — "
    "real amounts that would silently vanish from the fill. For EACH orphan decide, using "
    "the template lines' own definitions:\n"
    "- add_to with a FILLED template line: the orphan's amount belongs INSIDE that line "
    "(it qualifies per the line's definition) -> it is SUMMED into the fill.\n"
    "- add_to with an UNFILLED template line (listed under UNFILLED LINES): the orphan IS "
    "that line's data -> it becomes the line's source.\n"
    "- exclude: it genuinely belongs to no listed line (say why in reason).\n"
    "Never place one orphan into two lines. Never invent lines. When the family's TOTAL "
    "already fills a template total line, its components still belong on the template's "
    "COMPONENT lines — a statement shows both.\n"
    'Return ONLY JSON: {"placements":[{"series_id":"...","action":"add_to|exclude",'
    '"metric":"...|null","reason":"..."}]}'
)


def _transitive_rows(prec: dict, start: tuple[str, int], depth: int = 8) -> set[tuple[str, int]]:
    out, frontier = set(), {start}
    for _ in range(depth):
        nxt = set()
        for rr in frontier:
            for p in prec.get(rr, ()):  # noqa: B905
                if p not in out:
                    out.add(p)
                    nxt.add(p)
        if not nxt:
            break
        frontier = nxt
    return out


def place_orphans(gaps: list[dict], metric_maps, demand_metrics: list[dict],
                  snapshot: dict, context: str = "") -> tuple[int, int, list[dict]]:
    """One guarded LLM call assigning each orphan (meaning); deterministic apply
    with a CONTAINMENT GUARD (facts): an orphan may never be summed into a line
    whose existing series already contains it in the source's own graph (or vice
    versa) — that is arithmetic double-counting, whatever the model says.
    Placement onto an UNFILLED metric assigns it as that line's source instead.
    Returns (placed, excluded, leftover_orphans_for_review)."""
    from app.llm import MODEL_MAP, guarded_stream

    if not gaps:
        return 0, 0, []
    by_metric = {m.metric: m for m in metric_maps}
    meta = {m.get("metric"): m for m in demand_metrics}
    unfilled = [m for m in metric_maps if not m.series_id
                and m.metric in meta and m.status != "needs_decision"]

    blocks = []
    for g in gaps:
        cons = "; ".join(
            f"'{c['label']}' -> template line '{c['metric']}'"
            + (f" (def: {str(meta.get(c['metric'], {}).get('definition'))[:90]})"
               if meta.get(c["metric"], {}).get("definition") else "")
            for c in g["consumed"])
        head = (f"TOTAL '{g['total_label']}' ({g['sheet']} r{g['total_row']}"
                + (f", already fills template line '{g['total_metric']}'" if g.get("total_used") else "")
                + ") partitions into:")
        orp = "; ".join(f"{o['sid']} '{o['label']}'" for o in g["orphans"])
        blocks.append(f"{head}\n  consumed: {cons or '(only the total)'}\n  ORPHANS: {orp}")
    if unfilled:
        lines = "\n".join(
            f"  {m.metric} | {meta[m.metric].get('label') or m.metric}"
            + (f" | def: {str(meta[m.metric].get('definition'))[:90]}"
               if meta[m.metric].get("definition") else "")
            for m in unfilled[:20])
        blocks.append(f"UNFILLED LINES (template lines with no source yet):\n{lines}")
    user = ((f"TEMPLATE CONTEXT (authoritative):\n{context}\n\n" if context else "")
            + "\n\n".join(blocks) + "\n\nReturn the JSON now.")

    try:
        _, text = guarded_stream(model=MODEL_MAP, system=_PLACE_SYSTEM, content=user,
                                 max_tokens=6000, est_input_chars=len(_PLACE_SYSTEM) + len(user),
                                 temperature=0, site="conservation_placement")
        t = text.strip()
        a, b = t.find("{"), t.rfind("}")
        placements = (json.loads(t[a:b + 1]).get("placements") or []) if a != -1 else []
    except Exception as e:  # noqa: BLE001 — best-effort; leftovers become questions
        logger.warning("orphan placement failed (%s) — orphans go to review", e)
        placements = []

    prec = _row_precedents(snapshot)

    def _contained(sid_a: str, sid_b: str) -> bool:
        ra, rb = _sid_rows([sid_a]), _sid_rows([sid_b])
        if not ra or not rb:
            return False
        (a,), (b,) = ra, rb
        return b in _transitive_rows(prec, a) or a in _transitive_rows(prec, b)

    valid_orphans = {o["sid"]: o for g in gaps for o in g["orphans"]}
    placed = excluded = 0
    resolved: set[str] = set()
    for p in placements:
        sid = p.get("series_id")
        if sid not in valid_orphans or sid in resolved:
            continue
        if p.get("action") == "add_to":
            mm = by_metric.get(p.get("metric"))
            if mm is None:
                continue
            if mm.series_id:
                # SUM into an existing line — containment guard: never add a
                # component to a line already carrying its total (or vice versa).
                members = [mm.series_id, *(mm.also_series_ids or [])]
                if any(_contained(sid, m) for m in members):
                    logger.info("containment guard: refused %s into '%s' (already inside %s)",
                                sid, mm.metric, members)
                    continue
                if _SID.match(sid) and _SID.match(mm.series_id) \
                        and _SID.match(sid).group(1) == _SID.match(mm.series_id).group(1) \
                        and sid not in members:
                    mm.also_series_ids = [*(mm.also_series_ids or []), sid]
                    mm.note = ((mm.note or "") +
                               f" [+ '{valid_orphans[sid]['label']}' per conservation: "
                               f"{str(p.get('reason'))[:80]}]").strip()
                    placed += 1
                    resolved.add(sid)
            else:
                # the orphan IS an unfilled line's data — assign, provisionally
                mm.series_id = sid
                mm.status = "reconcile"
                mm.confidence = max(mm.confidence or 0.0, 0.7)
                mm.assumption = (f"'{valid_orphans[sid]['label']}' assigned by family "
                                 f"conservation: {str(p.get('reason'))[:120]}")
                placed += 1
                resolved.add(sid)
        elif p.get("action") == "exclude":
            excluded += 1
            resolved.add(sid)

    leftovers = [o for sid, o in valid_orphans.items() if sid not in resolved]
    return placed, excluded, leftovers
