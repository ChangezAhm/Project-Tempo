"""FastAPI parser service.

Stateless structural parser for Project Tempo. One real endpoint:
  POST /parse/{template_id}  → extract structure, persist template_sheets.

Run:  uvicorn app.main:app --reload --port 8000   (from parser/)
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import apply_aspose_license, settings
from app.pipeline import (
    SheetNotFound,
    get_structure,
    impact,
    inspect,
    load_snapshot,
    parse_and_persist,
    run_structure,
)
from app.datamodel.persist import derive_and_persist, get_data_model
from app.supabase_client import TemplateNotFound
from app.understanding.persist import get_understanding, understand_and_persist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_aspose_licensed = apply_aspose_license()

app = FastAPI(title="Project Tempo — Parser Service")

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
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except SheetNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, f"No snapshot available (run /parse first): {e}")


# --- Layer 2: structure + impact -------------------------------------------

# Re-derive metric rows / fields / periods / section signals from the stored
# snapshot (no Aspose re-parse). Useful after improving the detectors.
@app.post("/analyze/structure/{template_id}", dependencies=[Depends(require_api_key)])
def analyze_structure_route(template_id: str) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return run_structure(template_id)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(404, f"No snapshot available (run /parse first): {e}")


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
def understand_route(template_id: str, max_sheets: int = 8) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return understand_and_persist(template_id, max_sheets=max_sheets)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
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


@app.get("/datamodel/{template_id}", dependencies=[Depends(require_api_key)])
def get_datamodel_route(template_id: str, sheet: str | None = None, limit: int = 2000) -> dict:
    if not settings.configured:
        raise HTTPException(503, "Parser not configured (missing Supabase service-role key)")
    try:
        return get_data_model(template_id, sheet=sheet, limit=limit)
    except TemplateNotFound as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Read failed: {e}")
