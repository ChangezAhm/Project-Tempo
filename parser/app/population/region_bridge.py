"""The bridge between mapping and authoring — the missing link the audit found.

Before this module, the two halves never talked: the mapper would mark a
region-shaped template metric ("Custom KPI grid") unavailable, while the source
series that SHOULD land there sat in unused_source_series, and the additions
path idled because nothing routed candidates to regions. Here:

  used_series()           — ONE source of truth for "already used" (series_id
                            AND also_series_ids — the old run.py set omitted the
                            aggregate components, enabling a double-write).
  region_hosted_metrics() — which demand metrics live inside a region's slots.
  rank_candidates()       — per-region-kind candidate ordering (adjustment
                            lexicon for adjustment_rows, %/ratio/count for
                            kpi_list, …). Orders, never filters to zero.
  route_additions()       — a region ACTIVATES when a metric it hosts came back
                            unavailable/needs_decision; activated regions get
                            ranked candidates; everything flows through
                            propose_additions (one shared placed-set).
  approved_addition_proposals() — replay proposals a human approved via the
                            review inbox (kind='addition', status='answered').
  addition_review_items() — file additions into the inbox: applied blank-slot
                            lines as informational items; occupied-label
                            proposals as one-tap approvals.

Pure except the two Supabase helpers (lazy imports, best-effort)."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_ADJUSTMENT_LEXICON = re.compile(
    r"(?i)add.?back|one.?off|one.?time|exceptional|non.?recurring|normali[sz]|pro.?forma|"
    r"run.?rate|restructur|transaction|integration|management fee|stock comp|adjust")
_RATE_UNIT_KINDS = {"percent", "ratio"}


def used_series(metric_maps) -> set[str]:
    """Every source series a mapping consumed — primary AND aggregate components.
    (The audit found run.py's local set omitted also_series_ids, so a series
    consumed inside an aggregate could be re-proposed as a new line: double-write.)"""
    out: set[str] = set()
    for m in metric_maps:
        if m.series_id:
            out.add(m.series_id)
        out.update(sid for sid in (m.also_series_ids or []) if sid)
    return out


def region_hosted_metrics(target_inputs: list[dict], regions: list[dict]) -> dict[int, set[str]]:
    """region index -> demand metric keys whose facts sit in that region's slot
    rows (same sheet). This is how "the Custom KPI grid metric" is tied back to
    the physical region that hosts it."""
    out: dict[int, set[str]] = {}
    for idx, region in enumerate(regions):
        slots = region.get("slots") or []
        rows = ({int(s["row"]) for s in slots}
                if slots else set(range(region["row_start"], region["row_end"] + 1)))
        sheet = region.get("sheet_name")
        keys = {f.get("canonical_metric") or f.get("metric_label")
                for f in target_inputs
                if f.get("sheet_name") == sheet and f.get("row") in rows}
        keys.discard(None)
        if keys:
            out[idx] = keys
    return out


def rank_candidates(region: dict, series_list) -> list:
    """Order candidates by fit for this region's KIND — ordering only, never a
    filter (a reviewer can reject a bad line; a silently-missing one is worse)."""
    kind = (region.get("kind") or "other").lower()

    def score(s) -> tuple:
        label = s.label or ""
        unit_kind = getattr(s.unit, "kind", None)
        if kind == "adjustment_rows":
            fit = 0 if _ADJUSTMENT_LEXICON.search(label) else 1
        elif kind == "kpi_list":
            fit = 0 if unit_kind in _RATE_UNIT_KINDS else 1
        elif kind == "chart_of_accounts":
            fit = 0 if unit_kind == "money" else 1
        else:
            fit = 0
        return (fit, s.sheet, s.row, s.id)

    return sorted(series_list, key=score)


def route_additions(catalogue: dict, metric_maps, regions: list[dict],
                    target_inputs: list[dict], *, max_per_region: int | None = None
                    ) -> tuple[list[dict], list[str]]:
    """Propose additions with the mapper's verdicts wired in. A region whose
    hosted metric came back unavailable/needs_decision is ACTIVATED: its
    candidates are the ranked unused series (the data that had nowhere to go).
    Non-activated regions keep the default pool. Returns (proposals, notes)."""
    from app.population.authoring import propose_additions

    used = used_series(metric_maps)
    unavailable_keys = {m.metric for m in metric_maps
                        if getattr(m, "status", "direct") in ("unavailable", "needs_decision")
                        and not m.series_id}
    hosted = region_hosted_metrics(target_inputs, regions)

    unused = [s for sid, s in catalogue.items() if sid not in used]
    region_candidates: dict[int, list[str]] = {}
    notes: list[str] = []
    for idx, keys in hosted.items():
        if keys & unavailable_keys:
            ranked = rank_candidates(regions[idx], unused)
            region_candidates[idx] = [s.id for s in ranked]
            notes.append(
                f"region {regions[idx].get('sheet_name')}!r{regions[idx].get('row_start')}"
                f"-r{regions[idx].get('row_end')} activated: hosted metric(s) "
                f"{sorted(keys & unavailable_keys)[:3]} had no direct source match — "
                "unused source series routed here")

    proposals, pnotes = propose_additions(catalogue, used, regions,
                                          max_per_region=max_per_region,
                                          region_candidates=region_candidates)
    return proposals, notes + pnotes


# --- review-inbox integration (I/O, best-effort) ------------------------------

def addition_review_items(applied: list[dict], pending_overwrites: list[dict],
                          source_label: str) -> list[dict]:
    """Inbox rows: informational items for lines that were WRITTEN (blank slots,
    per the 'write immediately, flagged' decision), one-tap approval items for
    occupied-label proposals (destructive -> approval-gated). Keys are value-free
    and stable across re-runs."""
    from app.review.items import make_item

    items: list[dict] = []
    for a in applied:
        items.append(make_item(
            source="populate", kind="judgment",
            question=(f"New line '{a.get('label')}' was added at {a.get('sheet_name')} "
                      f"row {a.get('row')} — keep it?"),
            why=f"Written into an extensible region from {source_label} ({a.get('cells_written')} cells).",
            affected={"sheets": [a.get("sheet_name")]},
            suggested_answer="keep",
        ))
    for p in pending_overwrites:
        items.append(make_item(
            source="populate", kind="addition",
            question=(f"Replace the {p.get('slot_mode')} label "
                      f"'{p.get('expected_label')}' at {p.get('sheet_name')} row {p.get('row')} "
                      f"with '{p.get('label')}' and fill its values?"),
            why=(f"The source ({source_label}) carries '{p.get('label')}' and the template "
                 "marks this row as changeable. Approving writes it on the next populate."),
            affected={"sheets": [p.get("sheet_name")]},
            suggested_answer="approve",
            check_spec={"type": "addition", "proposal": p},   # replayable payload
        ))
    return items


def approved_addition_proposals(version_id: str) -> list[dict]:
    """Proposals a human APPROVED via the inbox — replayed into apply on every
    subsequent populate (marked approved=True so apply's gate opens). Best-effort."""
    try:
        from app import supabase_client as sb
        items = sb.list_review_items(version_id)
    except Exception as e:  # noqa: BLE001
        logger.info("approved additions unavailable (%s)", e)
        return []
    out: list[dict] = []
    for it in items:
        if it.get("kind") != "addition" or it.get("status") != "answered":
            continue
        answer = str(((it.get("resolution") or {}).get("answer")) or "").strip().lower()
        if answer and not answer.startswith(("approve", "yes", "keep", "confirm")):
            continue   # an explicit non-approval answer
        proposal = ((it.get("check_spec") or {}).get("proposal")) or {}
        if proposal:
            out.append({**proposal, "approved": True})
    return out
