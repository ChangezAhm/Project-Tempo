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


# Reportable statement lines for the time-series view (config/instruction cells excluded).
_TS_INCLUDE = ("data", "sourced", "computed")


def _display_scenario(scenario: str | None) -> str:
    """The scenario a slot shows under in the Actual/Budget toggle. Unlabelled slots
    (derive defaults them to 'unknown') are the implicit REPORTED figures -> actual."""
    s = (scenario or "").strip().lower()
    if s == "budget":
        return "budget"
    if s in ("forecast", "plan", "outlook"):
        return "forecast"
    return "actual"


def _period_of(f: dict) -> tuple[str, dict] | None:
    """A fact's period as (stable_key, descriptor). None if it carries no period at
    all (a non-time-series cell, e.g. a single config input)."""
    date = f.get("parsed_date")
    label = f.get("period_label")
    idx = f.get("period_index")
    key = date or label or (f"#{idx}" if idx is not None else None)
    if key is None:
        return None
    return key, {"key": key, "date": date, "index": idx,
                 "label": label or date or (f"P{idx + 1}" if idx is not None else key)}


def _period_sort_key(p: dict):
    # chronological when dates exist; else by the timeline ordinal; undated last.
    if p.get("date"):
        return (0, str(p["date"]), 0)
    if p.get("index") is not None:
        return (1, "", p["index"])
    return (2, str(p.get("label") or ""), 0)


def timeseries_view(template_id: str) -> dict:
    """Project the data model into a TIME SERIES: per tab (only tabs that carry
    metrics), metrics as rows and periods as columns, with a scenario dimension
    (actual/budget/forecast) the caller can toggle. Pure reshape over the stored
    facts — the dimensions (period, scenario, sheet) already live on each fact; this
    lays them out in their natural financial-model shape. No values (the model holds
    the template's STRUCTURE); each slot is the template cell that period/scenario maps to."""
    dm = get_data_model(template_id, limit=30000)
    if not dm.get("available"):
        # The data model is built on derive (normally the first populate); a template
        # that's only been UNDERSTOOD has none yet. Derive it on demand — deterministic,
        # no LLM — so the time series works straight after analysis instead of erroring.
        try:
            derive_and_persist(template_id)
            dm = get_data_model(template_id, limit=30000)
        except Exception as e:  # noqa: BLE001 — e.g. no understanding to derive from yet
            logger.info("timeseries: data model unavailable and could not derive (%s)", e)
    if not dm.get("available"):
        return {"template_version_id": dm.get("template_version_id"),
                "available": False, "scenarios": [], "sheets": []}

    by_sheet: dict[str, list[dict]] = {}
    for f in dm.get("facts") or []:
        if (f.get("category") or "data") in _TS_INCLUDE:
            by_sheet.setdefault(f.get("sheet_name") or "", []).append(f)

    all_scen: set[str] = set()
    sheets_out: list[dict] = []
    for sheet, grp in by_sheet.items():
        periods: dict[str, dict] = {}
        metrics: dict[str, dict] = {}
        for f in grp:
            label = f.get("metric_label") or "(unlabelled)"
            m = metrics.get(label)
            if m is None:
                m = metrics[label] = {
                    "metric": f.get("canonical_metric") or label, "label": label,
                    "unit": f.get("unit"), "basis": f.get("basis"),
                    "category": f.get("category") or "data",
                    "definition": f.get("definition"),
                    "_row": f.get("row") or 0, "_col": f.get("col") or 0, "cells": {},
                }
            m["unit"] = m["unit"] or f.get("unit")
            r = f.get("row") or 0
            if r and (not m["_row"] or r < m["_row"]):
                m["_row"] = r
            per = _period_of(f)
            if per is None:
                continue
            key, desc = per
            periods.setdefault(key, desc)
            scen = _display_scenario(f.get("scenario"))
            all_scen.add(scen)
            m["cells"].setdefault(scen, {})[key] = f.get("cell")

        period_list = sorted(periods.values(), key=_period_sort_key)
        metric_list = sorted(metrics.values(), key=lambda x: (x["_row"], x["_col"]))
        sheet_scen = sorted({s for m in metric_list for s in m["cells"]})
        sheets_out.append({
            "sheet": sheet,
            "grain": _modal([f.get("period_type") for f in grp]) or "period",
            "is_timeseries": len(period_list) > 1,
            "periods": period_list,
            "scenarios": sheet_scen,
            "metrics": [{k: v for k, v in m.items() if not k.startswith("_")} for m in metric_list],
        })

    # tabs that actually carry a timeline first (the relevant ones), then the rest.
    sheets_out.sort(key=lambda s: (not s["is_timeseries"], s["sheet"]))
    return {
        "template_version_id": dm["template_version_id"],
        "available": True,
        "scenarios": sorted(all_scen),
        "sheets": sheets_out,
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
