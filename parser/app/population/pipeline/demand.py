"""Demand: what the template asks to be filled — distinct input metrics +
period/scenario shape — from the persisted data model, with the torn-model
guard (never populate from a partial write)."""

from __future__ import annotations

import logging
import re
from collections import Counter

from app.datamodel.derive import DERIVATION_VERSION
from app.datamodel.persist import derive_and_persist, get_data_model
from app.population.pipeline.state import RunState
from app.population.schema import metric_key

logger = logging.getLogger(__name__)

_ROWN = re.compile(r"^row \d+$")   # a pure positional fallback label (no metric identity)
_ISOTS = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")   # a date posing as a label


def _meaningless_key(key: str) -> bool:
    """A metric key that carries no meaning for the mapper: a positional
    fallback ('row 25') or a raw date/timestamp (a period-header or as-of cell
    that leaked in as an input). Such keys can never match a source series by
    name — they'd waste a mapping slot and pollute every audit."""
    return bool(_ROWN.match(key) or _ISOTS.match(key))


def build_demand(template_id: str, as_of_date: str | None) -> tuple[dict, list[dict]]:
    """The template's 'demand' (distinct input metrics + period/scenario shape) +
    the input facts to fill. Targets the data/sourced cells (the actual inputs),
    not computed/config/exclude."""
    def _torn(d: dict) -> bool:
        """A model whose stored facts fall short of what its own model row
        declares is a TORN write (a mid-insert failure) — populating from it
        silently drops whole statement blocks (a real incident filled 11 of 42
        metrics because only 500 of 2,092 facts survived a disconnect)."""
        declared = (d.get("model") or {}).get("fact_count")
        return bool(d.get("available") and declared
                    and len(d.get("facts") or []) < declared
                    and not d.get("facts_truncated"))

    dm = get_data_model(template_id, limit=30000)
    stored_ver = ((dm.get("model") or {}).get("dimensions") or {}).get("derivation_version", 0)
    # Auto-(re)derive when the data model is missing, was built by older logic,
    # OR is torn — so population always uses a complete, up-to-date map.
    if not dm.get("available") or stored_ver < DERIVATION_VERSION or _torn(dm):
        logger.info("data model for %s missing/stale/torn (v%s < v%s, torn=%s) — (re)deriving now",
                    template_id, stored_ver, DERIVATION_VERSION, _torn(dm))
        try:
            derive_and_persist(template_id)
        except Exception as e:  # noqa: BLE001
            if not dm.get("available"):
                raise RuntimeError(
                    f"Target has no data model and it can't be derived — run 'Understand' on the target first. ({e})"
                )
            if _torn(dm):
                # never fill from a torn model — a partial fill reads as a
                # terrible mapping when it's actually missing demand
                raise RuntimeError(
                    f"The target's data model is incomplete (a previous write was cut short) "
                    f"and re-deriving failed ({e}) — retry in a moment.")
            logger.warning("re-derive failed (%s) — using the existing (stale) data model", e)
        dm = get_data_model(template_id, limit=30000)
        if not dm.get("available"):
            raise RuntimeError("Could not build a data model for the target.")
        if _torn(dm):
            raise RuntimeError(
                "The target's data model is still incomplete after re-deriving — "
                "check parser/Supabase connectivity and retry.")
    fillable = [f for f in dm["facts"] if f.get("category") in ("data", "sourced")]
    # value_role guard: a total/subtotal/header row is the TEMPLATE'S arithmetic —
    # even when its cells are literals it must be neither cleared nor written.
    _ROLE_PROTECTED = ("total", "subtotal", "header")
    inputs = [f for f in fillable
              if (f.get("value_role") or "").strip().lower() not in _ROLE_PROTECTED]
    protected_totals = len(fillable) - len(inputs)
    # sheet-role write gate accounting — how many input-looking cells were blocked
    # (category='staging'); surfaced so a smaller fill explains itself.
    gated_cells = sum(1 for f in dm["facts"] if f.get("category") == "staging")
    metrics: dict[str, dict] = {}
    for f in inputs:
        key = metric_key(f)
        # A meaningless key (positional 'row 25' / raw date) generates no mapping
        # demand. The cell stays a fact (it's still an input in the model); it
        # just doesn't waste a mapping slot until it earns a real metric identity.
        if key and _meaningless_key(key):
            continue
        if key and key not in metrics:
            # definition/qualification_criteria/expected_source are the L3 business
            # logic — the mapper needs them to tell 'Adjusted' from 'Reported', to
            # refuse a series that fails the template's own qualification rules,
            # and to prefer/refuse a source of the wrong provenance.
            metrics[key] = {"metric": key, "label": f.get("metric_label"), "unit": f.get("unit"),
                            "sign_convention": f.get("sign_convention"),
                            "definition": f.get("definition"),
                            "qualification_criteria": f.get("qualification_criteria"),
                            "expected_source": f.get("expected_source")}
    # period_index is a PER-SHEET ordinal, so the count used for positional
    # alignment must be per-sheet too — a global max would misalign sheets whose
    # timelines are shorter than the longest one in the workbook.
    period_count_by_sheet: dict[str, int] = {}
    for f in inputs:
        if f.get("period_index") is not None:
            s = f["sheet_name"]
            period_count_by_sheet[s] = max(period_count_by_sheet.get(s, 0), f["period_index"] + 1)
    period_count = max(period_count_by_sheet.values(), default=0)
    scenarios = sorted({f["scenario"] for f in inputs if f.get("scenario") and f["scenario"] != "unknown"})
    # DOMINANT grain, not alphabetical: a single YTD/annual column used to make
    # sorted()[0] say 'annual' for a monthly template, sending the dateless
    # positional-alignment fallback hunting for year columns.
    grain_votes = Counter(f.get("period_type") for f in inputs if f.get("period_type"))
    stored = (dm["model"] or {}).get("period_grains") or ["monthly"]
    period_grain = grain_votes.most_common(1)[0][0] if grain_votes else stored[0]
    demand = {"as_of_date": as_of_date, "period_count": period_count,
              "period_count_by_sheet": period_count_by_sheet,
              "period_grain": period_grain,
              "scenarios": scenarios, "metrics": list(metrics.values()),
              "gated_cells": gated_cells, "protected_totals": protected_totals}
    return demand, inputs


def stage_demand(state: RunState) -> None:
    state.demand, state.target_inputs = build_demand(state.target_template_id,
                                                     state.as_of_date)
