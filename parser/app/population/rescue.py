"""Deep per-metric rescue pass.

The main mapping (mapping.py) is ONE batched call over ALL template metrics — cheap
and good on the easy ones, but each metric gets only a sliver of the model's
attention. This pass gives EACH metric the first pass could not place its OWN focused
agent: one metric, the entire source catalogue, run in parallel. Each agent returns a
single considered decision (the same status taxonomy as mapping) — a find, a
reconciliation, an ask, or a well-reasoned 'unavailable'.

Guarded by the run's spend cap and best-effort: a metric whose agent errors or is
capped is simply left exactly as the main pass had it. Concurrency is bounded, and
because the spend guard lives in a ContextVar (which does NOT cross threads) each
worker re-binds it so every rescue call is still firewalled.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os

from app.llm import MODEL_MAP, guarded_stream
from app.population.catalogue import Series
from app.population.cost import SpendCapExceeded, get_guard, set_guard
from app.population.mapping import _metric_lines, _parse, _series_lines
from app.population.schema import MetricMap

logger = logging.getLogger(__name__)

# Sonnet by default — the depth here is per-metric FOCUS, not a bigger model. Bump to
# claude-opus-4-8 via env if you raise the cap and want maximum reasoning.
MODEL_RESCUE = os.environ.get("TEMPO_MODEL_RESCUE", MODEL_MAP)
_MAX_WORKERS = int(os.environ.get("TEMPO_RESCUE_WORKERS", "8"))

_SYSTEM = (
    "You are resolving ONE template metric that the first (fast, batched) mapping pass "
    "could not place. You see that metric and the ENTIRE source catalogue. Think hard "
    "before concluding nothing fits — can any source series populate it:\n"
    "- direct: one series means the same thing.\n"
    "- aggregate: it is the EXACT sum of several source series (series_id + also_series_ids).\n"
    "- reconcile: the source has the same amount cut DIFFERENTLY (a combined line the "
    "template splits; a functional split the template wants by nature -> SUM the "
    "functional lines onto the residual/other line; a total for a component). Fill it "
    "provisionally with a plain-English `assumption`.\n"
    "- needs_decision: related data exists but assigning it is a genuine coin-flip -> "
    "series_id null, put the question+options in `assumption`.\n"
    "- unavailable: the source truly has no data for it, even at a different cut -> "
    "series_id null, say why in `note`.\n"
    "PREFER source series not already used by another line; you MAY still reconcile onto a "
    "residual line that legitimately owns them, but NEVER double-count (each source series "
    "belongs to at most one template line). The value always comes from a real series — "
    "never invent numbers.\n"
    "You decide the COMPLETE fill semantics (deterministic code executes, it does not "
    "re-decide): rollup — months -> quarter/year when grains differ: 'sum' (period "
    "flows), 'end' (point-in-time stocks: balances, headcount, ARR/run-rates), 'avg' "
    "(rates/percentages). source_unit/target_unit — the unit strings AS READ from each "
    "side (\"USD'000\", 'EUR m', '%', 'FTE'); the executor computes the scale — never "
    "state a factor. sign_flip + sign_basis (one line of evidence). period_map — "
    "'calendar' unless a side is dateless.\n"
    'Return ONE JSON object: {"mappings":[{"metric":"<same key>","status":"direct|aggregate'
    '|reconcile|needs_decision|unavailable","series_id":"...|null","also_series_ids":[],'
    '"assumption":"...|null","rollup":"sum|end|avg","source_unit":"...|null",'
    '"target_unit":"...|null","sign_flip":false,"sign_basis":"...|null",'
    '"period_map":"calendar|positional","confidence":0.0,"note":"..."}]}'
)


def _user(metric: dict, series_block: str, used_block: str, context: str) -> str:
    ctx = f"TEMPLATE CONTEXT (authoritative — sponsor-confirmed):\n{context}\n\n" if context else ""
    return (
        f"{ctx}"
        "SOURCE SERIES (id | sheet | label [unit] | samples):\n"
        f"{series_block}\n\n"
        "ALREADY USED by other template lines (do not double-count; a residual line may "
        f"still legitimately aggregate them): {used_block}\n\n"
        "THE ONE METRIC TO RESOLVE (key | label | unit | def | qualifies):\n"
        f"{_metric_lines([metric])}\n\n"
        "Return the JSON now."
    )


def rescue_metrics(metrics: list[dict], catalogue: dict[str, Series], *,
                   used_series: set[str] | None = None, context: str = "",
                   model: str | None = None, max_workers: int | None = None) -> list[MetricMap]:
    """One focused agent per metric, run in parallel. Returns the MetricMaps the agents
    produced (order not significant; the caller overlays them onto the main mapping).
    Best-effort: a failed or spend-capped agent yields no map for that metric."""
    if not metrics or not catalogue:
        return []
    model = model or MODEL_RESCUE
    workers = max(1, min(max_workers or _MAX_WORKERS, len(metrics)))
    series_block = _series_lines(catalogue)
    used_block = ", ".join(sorted(used_series)) if used_series else "(none yet)"
    guard = get_guard()   # capture the parent run's guard to re-bind inside each worker

    def _one(metric: dict) -> MetricMap | None:
        if guard is not None:
            set_guard(guard)   # ContextVar does not cross threads — firewall each call
        try:
            _, text = guarded_stream(model=model, system=_SYSTEM,
                                     content=_user(metric, series_block, used_block, context),
                                     max_tokens=1200, temperature=0,
                                     site=f"metric_rescue:{metric.get('metric')}")
            maps = _parse(text)
        except SpendCapExceeded:
            logger.warning("rescue hit the spend cap — leaving '%s' as the main pass had it",
                           metric.get("metric"))
            return None
        except Exception:  # noqa: BLE001 — one agent failing must not sink the rest
            logger.exception("rescue agent failed for '%s'", metric.get("metric"))
            return None
        if not maps:
            return None
        mm = maps[0]
        mm.metric = metric.get("metric")   # trust OUR key, not the model's echo
        return mm

    out: list[MetricMap] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_one, metrics):
            if r is not None:
                out.append(r)
    return out
