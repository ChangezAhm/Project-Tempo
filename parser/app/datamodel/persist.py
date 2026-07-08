"""Persist the derived data model and serve it.

`derive_and_persist` runs the deterministic derivation and writes the facts +
summary (job-tracked, idempotent replace). `get_data_model` reads it back for
inspection / the (later) review UI. Layer-4 LLM enrichment of ambiguous
dimensions and the contract/correction merge come next.
"""

from __future__ import annotations

import logging
from collections import Counter

from app import supabase_client as sb
from app.datamodel.derive import DERIVATION_VERSION, derive_data_model
from app.datamodel.merge import apply_corrections

logger = logging.getLogger(__name__)


def derive_and_persist(template_id: str) -> dict:
    version_id, _, _ = sb.get_latest_file(template_id)
    job_id = sb.create_job(version_id, job_type="datamodel")
    try:
        result = derive_data_model(template_id)
        fact_dicts = [f.model_dump(mode="json") for f in result.facts]

        # Re-apply the template's stored corrections to this version's facts.
        corrections = sb.list_corrections(template_id)
        fact_dicts, applied, unmatched = apply_corrections(fact_dicts, corrections)

        rows = [{**d, "template_version_id": version_id} for d in fact_dicts]
        sb.replace_rows("template_data_points", version_id, rows)

        dims = result.dimensions
        flags = list(dims.review_flags)
        if unmatched:
            flags.append(
                f"{len(unmatched)} stored correction(s) matched no fact in this version "
                f"(template may have changed): {[c.get('note') or c['id'] for c in unmatched]}"
            )
        dim_json = dims.model_dump(mode="json")
        dim_json["derivation_version"] = DERIVATION_VERSION   # for auto-re-derive-on-stale
        sb.upsert_data_model(version_id, {
            "archetype": dims.archetype,
            "timeline_relative": dims.timeline_relative,
            "base_currency": dims.base_currency,
            "fact_count": dims.fact_count,
            "scenarios": dims.scenarios,
            "period_grains": dims.period_grains,
            "entities": dims.entities,
            "review_flags": flags,
            "dimensions": dim_json,
        })
        summary = {
            "template_version_id": version_id,
            "fact_count": dims.fact_count,
            "scenarios": dims.scenarios,
            "period_grains": dims.period_grains,
            "base_currency": dims.base_currency,
            "corrections_applied": len(applied),
            "corrections_unmatched": len(unmatched),
            "review_flags": flags,
        }
        sb.complete_job(job_id, summary)
        return summary
    except Exception as e:
        sb.fail_job(job_id, str(e))
        raise


def get_contract(template_id: str) -> dict:
    version_id, _, _ = sb.get_latest_file(template_id)
    contract = sb.get_contract(template_id) or {"template_id": template_id, "status": "draft"}
    corrections = sb.list_corrections(template_id)
    model = (
        sb.get_client().table("template_data_model").select(
            "archetype, timeline_relative, base_currency, fact_count, scenarios, period_grains, review_flags")
        .eq("template_version_id", version_id).limit(1).execute().data
    )
    return {
        "template_id": template_id,
        "latest_version_id": version_id,
        "contract": contract,
        "corrections": corrections,
        "model": model[0] if model else None,
    }


# Categories population actually writes into (see derive._emit's category rules).
_FILLABLE = ("data", "sourced")


def _modal(values: list) -> object | None:
    """Most common non-empty value; ties broken by first occurrence (dicts keep
    insertion order and max() returns the first winner)."""
    counts: dict = {}
    for v in values:
        if v in (None, ""):
            continue
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _aggregate_fields(facts: list[dict]) -> list[dict]:
    """Fold per-cell facts into per-(sheet, metric) contract fields — the surface
    a reviewer confirms, one row per line item instead of one per cell."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for f in facts:
        # Unlabelled rows still need a reviewable group; don't silently drop them.
        label = f.get("metric_label") or "(unlabelled)"
        groups.setdefault((f.get("sheet_name") or "", label), []).append(f)

    keyed: list[tuple[tuple, dict]] = []
    for (sheet, label), grp in groups.items():
        ordered = sorted(grp, key=lambda f: (f.get("row") or 0, f.get("col") or 0))
        cats = Counter(f.get("category") or "data" for f in grp)
        first = ordered[0]
        field = {
            "sheet_name": sheet,
            "metric_label": label,
            "canonical_metric": _modal([f.get("canonical_metric") for f in grp]),
            "unit": _modal([f.get("unit") for f in grp]),
            "sign_convention": _modal([f.get("sign_convention") for f in grp]),
            "scenarios": sorted({s for f in grp if (s := f.get("scenario"))}),
            "category_counts": {k: n for k, n in cats.items() if n},
            "fillable_count": sum(n for k, n in cats.items() if k in _FILLABLE),
            "fact_count": len(grp),
            "cells": [f.get("cell") for f in ordered[:3]],
            "corrected": any(f.get("applied_correction_ids") for f in grp),
        }
        keyed.append(((sheet, first.get("row") or 0, first.get("col") or 0), field))
    keyed.sort(key=lambda kf: kf[0])
    return [f for _, f in keyed]


def get_contract_fields(template_id: str) -> dict:
    """The reviewable Template Contract surface: the data model grouped into
    per-(sheet, metric) fields. Pure aggregation over the stored facts — no LLM,
    no writes."""
    dm = get_data_model(template_id, limit=30000)
    if not dm.get("available"):
        return {"template_version_id": dm.get("template_version_id"),
                "available": False, "count": 0, "fields": []}
    fields = _aggregate_fields(dm.get("facts") or [])
    return {
        "template_version_id": dm["template_version_id"],
        "available": True,
        "count": len(fields),
        "fields": fields,
    }


def get_data_model(template_id: str, *, sheet: str | None = None, limit: int = 2000) -> dict:
    version_id, _, _ = sb.get_latest_file(template_id)
    client = sb.get_client()
    model = (
        client.table("template_data_model").select("*")
        .eq("template_version_id", version_id).limit(1).execute().data
    )
    if not model:
        return {"template_version_id": version_id, "available": False}

    # PostgREST caps a single response at ~1000 rows, so page through with range().
    facts: list[dict] = []
    page = 1000
    start = 0
    while start < limit:
        q = client.table("template_data_points").select("*").eq("template_version_id", version_id)
        if sheet:
            q = q.eq("sheet_name", sheet)
        chunk = (q.order("sheet_name").order("row").order("col")
                 .range(start, start + page - 1).execute().data or [])
        facts.extend(chunk)
        if len(chunk) < page:
            break
        start += page
    return {
        "template_version_id": version_id,
        "available": True,
        "model": model[0],
        "facts": facts[:limit],
        "facts_truncated": len(facts) >= limit,
    }
