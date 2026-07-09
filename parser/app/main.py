"""FastAPI parser service.

Stateless structural parser for Project Tempo. One real endpoint:
  POST /parse/{template_id}  → extract structure, persist template_sheets.

Run:  uvicorn app.main:app --reload --port 8000   (from parser/)
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from functools import partial

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app import supabase_client as sb
from app.config import apply_aspose_license, settings
from app.pipeline import (
    SheetNotFound,
    SnapshotUnavailable,
    get_structure,
    impact,
    inspect,
    load_snapshot,
    parse_and_persist,
    run_structure,
)
from app.datamodel.dimensions_llm import enrich_and_persist
from app.datamodel.persist import derive_and_persist, get_contract, get_contract_fields, get_data_model
from app.population.cost import SpendCapExceeded
from app.population.run import populate_from_bytes
from app.supabase_client import TemplateNotFound
from app.understanding.persist import get_understanding, understand_and_persist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_aspose_licensed = apply_aspose_license()

app = FastAPI(title="Project Tempo — Parser Service")


@app.on_event("startup")
def _reconcile_stale_jobs() -> None:
    """Jobs run inside the request; a restart mid-run orphans their rows at
    status='running'. Close them so the job table reflects reality."""
    if not settings.configured:
        return
    try:
        n = sb.fail_stale_jobs()
        if n:
            logger.warning("Closed %d orphaned analysis_jobs from a previous process", n)
    except Exception as e:  # noqa: BLE001 — reconciliation must never block startup
        logger.warning("Stale-job reconciliation skipped: %s", e)


# One expensive run (understand / populate) per template at a time. Two tabs or a
# double-click otherwise run concurrently over non-transactional delete-then-insert
# persistence and interleave rows. In-process only — matches the single-process
# uvicorn deployment.
_inflight: set[tuple[str, str]] = set()
_inflight_lock = threading.Lock()


@contextmanager
def _single_run(kind: str, template_id: str):
    key = (kind, template_id)
    with _inflight_lock:
        if key in _inflight:
            raise HTTPException(409, f"A {kind} run is already in progress for this template.")
        _inflight.add(key)
    try:
        yield
    finally:
        with _inflight_lock:
            _inflight.discard(key)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Gate data endpoints on a shared secret. No-op when PARSER_API_KEY is
    unset (local dev); enforced everywhere it's configured."""
    expected = settings.parser_api_key
    if expected and x_api_key != expected:
        raise HTTPException(401, "Missing or invalid X-API-Key")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "supabase_configured": settings.configured,
        "aspose_licensed": _aspose_licensed,
        "auth_required": bool(settings.parser_api_key),
    }


# Sync def → Starlette runs it in a worker thread, so the Aspose parse
# (CPU-bound, seconds) doesn't block the event loop.
@app.post("/parse/{template_id}", dependencies=[Depends(require_api_key)])
def parse(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return parse_and_persist(template_id)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001 - surface parse failures to the caller
        raise HTTPException(500, f"Parse failed: {e}")


# Read-only inspection — see the full extraction without persisting.
#   /inspect/{id}                      → workbook overview
#   /inspect/{id}?sheet=PortCo_Input   → that sheet's cells + precedents + validations
#   ...&formulas_only=true&limit=50    → just formula cells
@app.get("/inspect/{template_id}", dependencies=[Depends(require_api_key)])
def inspect_route(
    template_id: str,
    sheet: str | None = None,
    limit: int = 200,
    formulas_only: bool = False,
) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return inspect(template_id, sheet=sheet, limit=limit, formulas_only=formulas_only)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except SheetNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Inspect failed: {e}")


# Read the PERSISTED snapshot from Storage — no re-parse (proves Option B).
#   /snapshot/{id}                    → overview from the stored blob
#   /snapshot/{id}?sheet=PortCo_Input → that sheet's stored cells + validations
@app.get("/snapshot/{template_id}", dependencies=[Depends(require_api_key)])
def snapshot_route(
    template_id: str,
    sheet: str | None = None,
    limit: int = 200,
    formulas_only: bool = False,
) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return load_snapshot(template_id, sheet=sheet, limit=limit, formulas_only=formulas_only)
    except (TemplateNotFound, SheetNotFound, SnapshotUnavailable) as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001 — corrupt blob / auth failure is NOT a 404
        logger.exception("Snapshot read failed")
        raise HTTPException(500, f"Snapshot read failed: {e}")


# --- Layer 2: structure + impact -------------------------------------------

# Re-derive metric rows / fields / periods / section signals from the stored
# snapshot (no Aspose re-parse). Useful after improving the detectors.
@app.post("/analyze/structure/{template_id}", dependencies=[Depends(require_api_key)])
def analyze_structure_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return run_structure(template_id)
    except (TemplateNotFound, SnapshotUnavailable) as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001 — a detector bug is NOT a 404
        logger.exception("Structure analysis failed")
        raise HTTPException(500, f"Structure analysis failed: {e}")


# Read the persisted structure (optionally one sheet).
@app.get("/structure/{template_id}", dependencies=[Depends(require_api_key)])
def structure_route(template_id: str, sheet: str | None = None) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return get_structure(template_id, sheet=sheet)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Read endpoint failed")
        raise HTTPException(500, f"Read failed: {e}")


# Deterministic impact: change a cell → what's affected (downstream closure).
#   /impact/{id}?cell=Quarterly_Output!I6&depth=3
@app.get("/impact/{template_id}", dependencies=[Depends(require_api_key)])
def impact_route(template_id: str, cell: str, depth: int = 3, max_total: int = 50) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return impact(template_id, cell, depth=depth, max_total=max_total)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Impact failed: {e}")


# --- Layer 3: workbook understanding ---------------------------------------

# Run the LLM understanding (route → per-sheet → synthesize → verify), render
# input-area snippets, and persist it. Long-running (~minutes, multiple Opus
# calls); the request blocks until done. Re-running replaces the prior result.
@app.post("/understand/{template_id}", dependencies=[Depends(require_api_key)])
def understand_route(template_id: str, max_sheets: int = 16, force_deep: str | None = None) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    # force_deep: comma-separated sheet names the user demands a full pass for —
    # the veto path for a triage decision they disagree with.
    forced = {s.strip() for s in force_deep.split(",") if s.strip()} if force_deep else None
    try:
        with _single_run("understand", template_id):
            return understand_and_persist(template_id, max_sheets=max_sheets, force_deep=forced)
    except SpendCapExceeded as e:
        raise HTTPException(402, str(e))
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Understanding failed")
        raise HTTPException(500, f"Understanding failed: {e}")


# Read the persisted understanding for the UI: workbook summary, per-sheet
# understanding, and the ranked critical input areas (with signed snippet URLs).
@app.get("/understanding/{template_id}", dependencies=[Depends(require_api_key)])
def understanding_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return get_understanding(template_id)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Read endpoint failed")
        raise HTTPException(500, f"Read failed: {e}")


# --- Layer 4: dimensional data model ---------------------------------------

# Derive the data model (facts at metric/period/scenario coordinates) from the
# persisted L2 structure + L3 understanding. Deterministic + fast (no LLM yet).
@app.post("/datamodel/{template_id}", dependencies=[Depends(require_api_key)])
def datamodel_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return derive_and_persist(template_id)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Data model derivation failed: {e}")


# LLM-enrichment pass: auto-fill basis / canonical_metric / category for the
# ambiguous slots the deterministic derivation left unknown, stored as
# (fill-only, overridable) llm corrections, then re-derive. One Opus call.
@app.post("/datamodel/{template_id}/enrich", dependencies=[Depends(require_api_key)])
def enrich_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return enrich_and_persist(template_id)
    except SpendCapExceeded as e:
        raise HTTPException(402, str(e))
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Enrichment failed")
        raise HTTPException(500, f"Enrichment failed: {e}")


# --- Population: fill a template's inputs from a source workbook -----------

# Drag a data file onto a template. The raw workbook bytes are POSTed directly
# (application/octet-stream) — the source is parsed in-memory and NEVER stored
# as a template. LLM maps the source to the template's data-model inputs, then
# deterministic apply reads the real values + renders a filled workbook
# (download URL). Long-running (LLM) → offloaded to a worker thread.
@app.post("/populate/{target_template_id}", dependencies=[Depends(require_api_key)])
async def populate_route(
    target_template_id: str,
    request: Request,
    filename: str = "source.xlsx",
    as_of_date: str | None = None,
    dry_run: bool = False,
    display_unit: str | None = None,
    reset: str = "values",
    add_lines: str = "propose",
    deep_rescue: bool = True,
) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    if reset not in ("values", "full"):
        raise HTTPException(422, "reset must be 'values' or 'full'")
    if add_lines not in ("off", "propose", "apply"):
        raise HTTPException(422, "add_lines must be 'off', 'propose' or 'apply'")
    data = await request.body()
    if not data:
        raise HTTPException(400, "No source file in request body")
    try:
        with _single_run("populate", target_template_id):
            return await run_in_threadpool(
                partial(populate_from_bytes, target_template_id, filename, data, as_of_date,
                        display_unit=display_unit, reset=reset, add_lines=add_lines,
                        dry_run=dry_run, deep_rescue=deep_rescue)
            )
    except SpendCapExceeded as e:
        # 402: the run hit its spend cap and was aborted before overspending.
        raise HTTPException(402, str(e))
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Population failed")
        raise HTTPException(500, f"Population failed: {e}")


@app.get("/datamodel/{template_id}", dependencies=[Depends(require_api_key)])
def get_datamodel_route(template_id: str, sheet: str | None = None, limit: int = 2000) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return get_data_model(template_id, sheet=sheet, limit=limit)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Read endpoint failed")
        raise HTTPException(500, f"Read failed: {e}")


# --- Layer 4b: Template Contract + corrections -----------------------------

# The reviewable Contract grid: the data model grouped into field rows
# (sheet × label) so a human can approve/correct/exclude at metric level.
@app.get("/contract/{template_id}/fields", dependencies=[Depends(require_api_key)])
def contract_fields_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return get_contract_fields(template_id)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Contract fields read failed")
        raise HTTPException(500, f"Read failed: {e}")


# --- Authoring: extensible regions ------------------------------------------

# Detect the areas a template INVITES additions (blank KPI rows, 'Other…'
# blocks) and persist them. Cheap (Sonnet, text-only), guarded by the populate
# spend cap. Prerequisite for add-line-item population and the full reset.
@app.post("/authoring/regions/{template_id}", dependencies=[Depends(require_api_key)])
def detect_regions_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    from app.authoring.regions import detect_and_persist
    try:
        return detect_and_persist(template_id)
    except SpendCapExceeded as e:
        raise HTTPException(402, str(e))
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Region detection failed")
        raise HTTPException(500, f"Region detection failed: {e}")


@app.get("/authoring/regions/{template_id}", dependencies=[Depends(require_api_key)])
def get_regions_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    from app.authoring.regions import get_regions
    try:
        return get_regions(template_id)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Regions read failed")
        raise HTTPException(500, f"Read failed: {e}")


# --- Review items: the questions the system asks, made answerable -----------

# List the template's review items (open first). The inbox for everything the
# system is unsure about: understanding flags, unverified impact chains,
# triage decisions.
@app.get("/review/{template_id}", dependencies=[Depends(require_api_key)])
def review_list_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        version_id, _, _ = sb.get_latest_file(template_id)
        items = sb.list_review_items(version_id)
        return {"template_version_id": version_id, "count": len(items),
                "open_count": sum(1 for i in items if i.get("status") == "open"),
                "items": items}
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Review list failed")
        raise HTTPException(500, f"Read failed: {e}")


# Backfill items from the ALREADY-STORED understanding (templates understood
# before this feature existed) — no LLM, no re-understand.
@app.post("/review/{template_id}/build", dependencies=[Depends(require_api_key)])
def review_build_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    from app.review.items import build_and_persist
    try:
        return build_and_persist(template_id)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Review build failed")
        raise HTTPException(500, f"Build failed: {e}")


# "Verify for me": run the deterministic dependency-graph check behind a
# machine-checkable question and store the verdict + evidence. No LLM.
@app.post("/review/{template_id}/items/{item_id}/verify", dependencies=[Depends(require_api_key)])
def review_verify_route(template_id: str, item_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    from app.review.verify import verify_item
    try:
        return verify_item(template_id, item_id)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Review verify failed")
        raise HTTPException(500, f"Verify failed: {e}")


# Answer / dismiss / reopen an item. An answer is durable knowledge: it stays
# on the item and (optionally, via the corrections endpoints) becomes a
# data-model correction.
@app.patch("/review/{template_id}/items/{item_id}", dependencies=[Depends(require_api_key)])
def review_answer_route(template_id: str, item_id: str, body: dict = Body(default={})) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    status = body.get("status")
    if status not in ("answered", "dismissed", "open"):
        raise HTTPException(422, "status must be 'answered', 'dismissed' or 'open'")
    try:
        item = sb.get_review_item(item_id)
        if item is None:
            raise HTTPException(404, "No such review item")
        from datetime import datetime, timezone
        resolution = None
        if status == "answered":
            resolution = {"answer": body.get("answer") or "", "resolved_by": "user",
                          "resolved_at": datetime.now(timezone.utc).isoformat()}
        elif status == "dismissed":
            resolution = {"reason": body.get("reason") or "", "resolved_by": "user",
                          "resolved_at": datetime.now(timezone.utc).isoformat()}
        return sb.update_review_item(item_id, {"status": status, "resolution": resolution})
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Review update failed")
        raise HTTPException(500, f"Update failed: {e}")


# Read the contract: status, the template-level corrections, and the model
# summary (the reviewable surface).
@app.get("/contract/{template_id}", dependencies=[Depends(require_api_key)])
def contract_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return get_contract(template_id)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Read endpoint failed")
        raise HTTPException(500, f"Read failed: {e}")


# Set contract status / notes. status="approved" pins the current latest version.
@app.patch("/contract/{template_id}", dependencies=[Depends(require_api_key)])
def patch_contract_route(template_id: str, body: dict = Body(default={})) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        fields: dict = {}
        if "notes" in body:
            fields["notes"] = body["notes"]
        if "status" in body:
            fields["status"] = body["status"]
            if body["status"] == "approved":
                fields["approved_version_id"] = sb.get_latest_file(template_id)[0]
        return sb.upsert_contract(template_id, fields)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Update failed: {e}")


# Add a correction (content match + patch). Re-run POST /datamodel to apply it.
#   body: {match: {...}, patch: {...}, note?, target?, created_by?}
@app.post("/datamodel/{template_id}/corrections", dependencies=[Depends(require_api_key)])
def add_correction_route(template_id: str, body: dict = Body(...)) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    if not isinstance(body.get("match"), dict) or not isinstance(body.get("patch"), dict):
        raise HTTPException(422, "Correction requires object `match` and `patch`.")
    try:
        return sb.add_correction(template_id, {
            "target": body.get("target", "fact"),
            "match": body["match"],
            "patch": body["patch"],
            "note": body.get("note"),
            "created_by": body.get("created_by"),
        })
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Add correction failed: {e}")


@app.delete("/datamodel/{template_id}/corrections/{correction_id}", dependencies=[Depends(require_api_key)])
def delete_correction_route(template_id: str, correction_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        sb.supersede_correction(correction_id)
        return {"superseded": correction_id}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Delete failed: {e}")
