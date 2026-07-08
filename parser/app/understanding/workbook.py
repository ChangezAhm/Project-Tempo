"""Phase 3 — workbook-level understanding: route → per-sheet (parallel) →
synthesize (LLM) → verify (deterministic, graph-grounded).

Turns the independent per-sheet maps into one coherent template understanding:
archetype, input surface, cross-sheet data flow, metric reconciliation,
workbook rules, and impact chains — with the cross-sheet claims checked
against the actual dependency graph.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langsmith import traceable

from app import supabase_client as sb
from app.llm import MODEL, get_client
from app.population import source_cache
from app.population.cost import (
    SpendCapExceeded, SpendGuard, default_onboarding_cap_usd, estimate_call_usd,
    get_guard, set_guard,
)
from app.pipeline import _cell_rc, build_dependents_index, trace_impact
from app.understanding.per_sheet import _extract_json, to_strict_schema, understand_sheet
from app.understanding.prompts import SYNTHESIZE_SYSTEM, build_synth_user
from app.understanding.run import _annotations, _hints, _workbook_ctx
from app.understanding.schema import SheetUnderstanding, WorkbookUnderstanding
from app.understanding.sheet_image import render_sheet_tiles

logger = logging.getLogger(__name__)

_MAX_SHEET_ATTEMPTS = 3
# 429s are the expected failure mode with parallel Opus+vision workers — a
# rate-limited sheet must be retried, never dropped as a permanent failure.
_TRANSIENT = ("connection", "peer closed", "incomplete chunked", "timeout", "timed out",
              "overloaded", "econnreset", "reset by peer", "503", "502", "529", "429",
              "rate_limit", "too many requests", "remote end closed")


def _is_transient(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return any(t in s for t in _TRANSIENT)


_DUMP_NAME_RE = re.compile(r"(?i)(pbi|raw|dump|backup|_old|^old|depr)")
_SYNTH_SCHEMA = to_strict_schema(WorkbookUnderstanding)

# Per-sheet results are cached on disk BEFORE synthesis so a crash at the end
# (synthesis + its one corrective retry both failing) doesn't discard the
# expensive Opus+vision calls — a re-run serves them from cache for free. The
# key includes the template version, so a re-uploaded template never reuses old
# results. Bump this constant whenever prompts.SYSTEM or the SheetUnderstanding
# schema changes shape — that invalidates every cached result built under them.
_SHEET_CACHE_VERSION = 1


def _sheet_cache_key(version_id: str, sheet_name: str) -> str:
    # Sheet names can carry characters unsafe in a filename; the slug keeps the
    # key readable and the name-hash suffix keeps distinct names from colliding
    # after slugging.
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", sheet_name)
    tag = source_cache.content_hash(sheet_name.encode("utf-8"))[:8]
    return f"sheet-und-{version_id}-{slug}-{tag}-v{_SHEET_CACHE_VERSION}"


# --- cross-sheet dependency edges (shared by route, synth-context, verify) ---

def _cross_sheet_edges(snap: dict) -> tuple[set[tuple[str, str]], dict[str, int]]:
    """Returns ({(from, to)} where `to` reads from `from`, read_by_count[sheet])."""
    edges: set[tuple[str, str]] = set()
    read_by: dict[str, int] = {}
    for s in snap.get("sheets", []):
        name = s["name"]
        for c in s.get("cells", []):
            for rng in c.get("precedents", []):
                i = rng.rfind("!")
                if i == -1:
                    continue
                ref = rng[:i].strip("'")
                if ref != name:
                    edges.add((ref, name))          # name reads from ref → flow ref→name
                    read_by[ref] = read_by.get(ref, 0) + 1
    return edges, read_by


# --- 1. Route -------------------------------------------------------------

def route_sheets(snap: dict) -> list[dict]:
    """Deterministically decide which sheets get the (expensive) per-sheet pass.
    Skips hidden, empty, and large formula-less data dumps. Orders by importance."""
    _, read_by = _cross_sheet_edges(snap)
    routes: list[dict] = []
    for s in snap.get("sheets", []):
        name = s["name"]
        cells = s.get("cells", [])
        cell_count = len(cells)
        formula_count = sum(1 for c in cells if c.get("formula"))
        rb = read_by.get(name, 0)

        if s.get("is_hidden"):
            deep, reason = False, "hidden"
        elif cell_count == 0:
            deep, reason = False, "empty"
        elif formula_count == 0 and cell_count > 800 and (rb > 0 or _DUMP_NAME_RE.search(name)):
            deep, reason = False, "data dump"
        else:
            deep, reason = True, "content sheet"

        score = rb + formula_count // 50 + (1 if formula_count == 0 and rb else 0)
        routes.append({
            "sheet": name, "deep": deep, "reason": reason, "score": score,
            "cells": cell_count, "formulas": formula_count, "read_by": rb,
        })
    routes.sort(key=lambda r: (r["deep"], r["score"]), reverse=True)
    return routes


# --- 2. Synthesize --------------------------------------------------------

def _compact(u) -> dict:
    return {
        "sheet": u.sheet_name,
        "role": u.role.value,
        "summary": u.summary,
        "label_columns": u.label_columns,
        "sections": [{"title": s.title, "type": s.section_type.value, "range": s.cell_range} for s in u.sections],
        "key_metrics": [
            {"cell": m.label_cell, "label": m.label_as_written, "canonical": m.canonical_metric,
             "role": m.value_role.value, "unit": m.unit}
            for m in u.metric_rows[:40]
        ],
        "periods": [{"label": p.label, "status": p.status, "granularity": p.granularity} for p in u.periods],
        "input_field_count": len(u.input_fields),
        "author_rules": [{"category": r.rule_category.value, "summary": r.summary, "strict": r.is_strict}
                         for r in u.author_rules],
    }


def _call_synth(user_text: str, max_tokens: int) -> tuple[object, str]:
    guard = get_guard()
    if guard is not None:
        guard.check(estimate_call_usd(MODEL, len(SYNTHESIZE_SYSTEM) + len(user_text), max_tokens))
    with get_client().messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        system=SYNTHESIZE_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
    ) as stream:
        msg = stream.get_final_message()
    if guard is not None and getattr(msg, "usage", None) is not None:
        guard.record_actual(MODEL, msg.usage.input_tokens, msg.usage.output_tokens)
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"Synthesis truncated at max_tokens={max_tokens} — raise it.")
    return msg, next((b.text for b in msg.content if b.type == "text"), "")


@traceable(name="synthesize_workbook", run_type="chain")
def synthesize(snap: dict, understandings: list, *, max_tokens: int = 32000) -> tuple[WorkbookUnderstanding, dict]:
    edges, _ = _cross_sheet_edges(snap)
    edges_str = "\n".join(f"{a} -> {b}" for a, b in sorted(edges))
    named = "; ".join(
        f"{n['name']} -> {','.join(n.get('destinations', []))}"
        for n in snap.get("named_ranges", [])[:80]
    )
    user = build_synth_user(
        json.dumps([_compact(u) for u in understandings]),
        edges_str, named, json.dumps(_SYNTH_SCHEMA),
    )
    msg, text = _call_synth(user, max_tokens)
    try:
        wb = WorkbookUnderstanding.model_validate(json.loads(_extract_json(text)))
    except Exception as e:  # one corrective retry
        logger.warning("Synthesis parse failed (%s); retrying", e)
        msg, text = _call_synth(
            user + f"\n\nThat did not parse as valid WorkbookUnderstanding JSON: {e}. "
            "Return ONLY the corrected JSON object.", max_tokens,
        )
        wb = WorkbookUnderstanding.model_validate(json.loads(_extract_json(text)))
    return wb, {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens}


# --- 3. Verify (deterministic, graph-grounded) ----------------------------

def verify(wb: WorkbookUnderstanding, snap: dict) -> dict:
    """Check the cross-sheet claims against the real dependency graph; annotate
    graph_supported and surface unverified claims into review_flags."""
    edges, _ = _cross_sheet_edges(snap)
    for e in wb.data_flow:
        e.graph_supported = (e.from_sheet, e.to_sheet) in edges

    index = build_dependents_index(snap)
    output_cells = set(snap.get("formula_graph", {}).get("output_cells", []))
    for ch in wb.impact_chains:
        claimed = {f.split("!", 1)[0].strip("'") for f in ch.flows_to}
        rc = _cell_rc(ch.start)

        # (a) cell-level: does the start cell's downstream actually reach a claimed sheet?
        cell_ok = False
        if rc:
            res = trace_impact(index, output_cells, {}, ch.start, depth=4, max_total=300)
            affected = {a["sheet"] for a in res["affected"] if a["sheet"]}
            cell_ok = bool(claimed & affected) if claimed else res["affected_count"] > 0

        # (b) sheet-level fallback: the start's sheet flows to a claimed sheet in the
        # graph. Cross-sheet links via named ranges / Power-BI calls don't show up in
        # cell-level precedents, so this avoids false negatives while staying grounded.
        start_sheet = rc[0] if rc else (ch.start.split("!", 1)[0].strip("'") if "!" in ch.start else None)
        sheet_ok = bool(start_sheet) and any((start_sheet, c) in edges for c in claimed)

        ch.graph_supported = cell_ok or sheet_ok

    bad_flows = [f"{e.from_sheet}->{e.to_sheet}" for e in wb.data_flow if e.graph_supported is False]
    bad_chains = [ch.name for ch in wb.impact_chains if ch.graph_supported is False]
    if bad_flows:
        wb.review_flags.append(f"Data-flow claims NOT supported by the dependency graph: {bad_flows}")
    if bad_chains:
        wb.review_flags.append(f"Impact chains NOT confirmed by the dependency graph: {bad_chains}")

    return {
        "data_flow": {"supported": sum(1 for e in wb.data_flow if e.graph_supported), "total": len(wb.data_flow)},
        "impact_chains": {"supported": sum(1 for c in wb.impact_chains if c.graph_supported), "total": len(wb.impact_chains)},
    }


# --- Orchestrator ---------------------------------------------------------

@traceable(name="understand_workbook", run_type="chain")
def understand_workbook(template_id: str, *, max_sheets: int = 16, per_sheet_workers: int = 4) -> dict:
    # Arm the spend firewall for onboarding (Opus + per-sheet vision — legitimately
    # costs more than a populate, so it has its own higher ceiling). Every LLM call
    # below — per-sheet (in worker threads) and synthesis — checks against it.
    set_guard(SpendGuard(default_onboarding_cap_usd()))

    version_id, storage_path, filename = sb.get_latest_file(template_id)
    try:
        snap = json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    except Exception:  # noqa: BLE001 — storage 'not found' arrives as a generic error
        # A fresh upload that was never parsed (or a deleted snapshot). Running
        # 'Understand' implies parse-if-needed — self-heal instead of a raw
        # storage 404, the same way populate auto-derives a missing data model.
        logger.info("no snapshot for version %s — running parse first", version_id)
        from app.pipeline import parse_and_persist  # local: avoid an import cycle
        parse_and_persist(template_id)
        snap = json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    by_name = {s["name"]: s for s in snap["sheets"]}

    routes = route_sheets(snap)
    deep_all = [r["sheet"] for r in routes if r["deep"]]
    deep = deep_all[:max_sheets]
    # Content sheets beyond the cap are excluded entirely — that must never
    # happen silently, so they get a review flag (like failed_sheets) below.
    skipped_sheets = deep_all[max_sheets:]

    # Download the workbook ONCE; render each routed sheet's image (sequential —
    # Aspose isn't concurrency-safe), then fan out the LLM calls.
    data = sb.download_workbook(storage_path)
    fd, name = tempfile.mkstemp(suffix=Path(filename).suffix or ".xlsx")
    os.close(fd)
    tmp = Path(name)
    jobs = []
    try:
        tmp.write_bytes(data)
        for sheet_name in deep:
            sheet = by_name[sheet_name]
            if source_cache.get(_sheet_cache_key(version_id, sheet_name)) is not None:
                # Cached result — _run serves it without an Opus call, so the
                # render (the only other per-sheet cost) would be thrown away.
                imgs = []
            else:
                try:
                    imgs = render_sheet_tiles(tmp, sheet_name)
                except Exception as e:  # noqa: BLE001 — image is optional; fall back to the text grid
                    logger.warning("image render failed for %s (%s) — understanding text-only", sheet_name, e)
                    imgs = []
            jobs.append((
                sheet, imgs,
                _annotations(sheet), _workbook_ctx(snap), _hints(snap, sheet_name),
            ))
    finally:
        tmp.unlink(missing_ok=True)

    def _run(job):
        key = _sheet_cache_key(version_id, job[0]["name"])
        cached = source_cache.get(key)
        if cached is not None:
            try:
                result = {
                    "understanding": SheetUnderstanding.model_validate(cached["understanding"]),
                    "grounding": cached.get("grounding") or {},
                    # Replays the tokens the cached call originally cost, so the
                    # persisted usage still reflects what the understanding cost
                    # to produce (no new spend happens on this path).
                    "usage": cached["usage"],
                }
            except Exception as e:  # noqa: BLE001 — a corrupt entry falls through to a live call
                logger.warning("per-sheet cache entry unusable for %s (%s) — calling live", job[0]["name"], e)
            else:
                logger.info("per-sheet understanding cache HIT for %s — skipping Opus call", job[0]["name"])
                return result
        # Retry transient API/connection failures (flaky network, dropped streams,
        # overloaded, rate-limited) — these are common with large image payloads
        # and parallel workers, and shouldn't lose a whole sheet. A genuine error
        # fails after the retries.
        for attempt in range(_MAX_SHEET_ATTEMPTS):
            try:
                res = understand_sheet(*job)
                source_cache.put(key, {
                    "understanding": res["understanding"].model_dump(mode="json"),
                    "grounding": res["grounding"],
                    "usage": res["usage"],
                })
                return res
            except SpendCapExceeded:
                raise  # the cap is a hard stop, never a per-sheet "failure" to retry/swallow
            except Exception as e:  # noqa: BLE001
                transient = _is_transient(e)
                if transient and attempt < _MAX_SHEET_ATTEMPTS - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                logger.warning("per-sheet understanding failed for %s (%stransient): %s",
                               job[0]["name"], "" if transient else "non-", e)
                return None

    # initializer propagates the spend guard into each worker — a contextvar set in
    # the main thread is NOT visible in pool workers, so without this the bulk of
    # the onboarding spend (per-sheet vision calls) would run UNCAPPED.
    with ThreadPoolExecutor(max_workers=per_sheet_workers,
                            initializer=set_guard, initargs=(get_guard(),)) as ex:
        results = list(ex.map(_run, jobs))

    sheet_results = [r for r in results if r]
    # A dropped sheet must never vanish silently — record which routed sheets
    # failed so it surfaces to the user (review_flags + the run summary).
    failed_sheets = [jobs[i][0]["name"] for i, r in enumerate(results) if not r]

    understandings = [r["understanding"] for r in sheet_results]
    # The grounding audit (cited addresses vs the real grid) travels with each
    # sheet so persist can store it — it's the consultant-trust evidence trail.
    groundings = {r["understanding"].sheet_name: r["grounding"] for r in sheet_results}
    in_tok = sum(r["usage"]["input_tokens"] for r in sheet_results)
    out_tok = sum(r["usage"]["output_tokens"] for r in sheet_results)

    wb, synth_usage = synthesize(snap, understandings)
    verify_summary = verify(wb, snap)
    if failed_sheets:
        wb.review_flags.append(
            "Per-sheet understanding FAILED for these routed sheets, so they are "
            f"EXCLUDED from this analysis: {failed_sheets}. Re-run to retry."
        )
    if skipped_sheets:
        wb.review_flags.append(
            f"Sheet cap reached (max_sheets={max_sheets}) — these content sheets were "
            f"EXCLUDED from this analysis: {skipped_sheets}. Raise max_sheets and re-run "
            "to include them."
        )

    return {
        "workbook": wb,
        "sheet_understandings": understandings,
        "sheet_groundings": groundings,
        "routes": routes,
        "deep_sheets": deep,
        "skipped_sheets": skipped_sheets,
        "failed_sheets": failed_sheets,
        "verify": verify_summary,
        "usage": {
            "input_tokens": in_tok + synth_usage["input_tokens"],
            "output_tokens": out_tok + synth_usage["output_tokens"],
        },
    }
