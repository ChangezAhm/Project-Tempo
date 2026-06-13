"""Thin Supabase wrapper (service-role) for the parser.

Reads the stored workbook out of private Storage and writes the structural
extraction (template_sheets) + job status (analysis_jobs) back.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache

from supabase import Client, create_client

from app.config import settings

logger = logging.getLogger(__name__)


class TemplateNotFound(Exception):
    """No version/file rows exist for the requested template_id."""


@lru_cache(maxsize=1)
def get_client() -> Client:
    if not settings.configured:
        raise RuntimeError(
            "Supabase not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY in parser/.env"
        )
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_latest_file(template_id: str) -> tuple[str, str, str]:
    """Return (version_id, storage_path, original_filename) for the newest version."""
    sb = get_client()

    ver = (
        sb.table("template_versions")
        .select("id, version_number")
        .eq("template_id", template_id)
        .order("version_number", desc=True)
        .limit(1)
        .execute()
    )
    if not ver.data:
        raise TemplateNotFound(f"No versions for template {template_id}")
    version_id = ver.data[0]["id"]

    f = (
        sb.table("template_files")
        .select("storage_path, original_filename")
        .eq("template_version_id", version_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not f.data:
        raise TemplateNotFound(f"No file for version {version_id}")
    return version_id, f.data[0]["storage_path"], f.data[0]["original_filename"]


def download_workbook(storage_path: str) -> bytes:
    sb = get_client()
    return sb.storage.from_(settings.storage_bucket).download(storage_path)


def create_job(version_id: str, job_type: str = "parse_structure") -> str:
    sb = get_client()
    res = (
        sb.table("analysis_jobs")
        .insert(
            {
                "template_version_id": version_id,
                "job_type": job_type,
                "status": "running",
                "started_at": _now(),
            }
        )
        .execute()
    )
    return res.data[0]["id"]


def complete_job(job_id: str, summary: dict) -> None:
    sb = get_client()
    sb.table("analysis_jobs").update(
        {"status": "completed", "completed_at": _now(), "summary": summary}
    ).eq("id", job_id).execute()


def fail_job(job_id: str, error: str) -> None:
    sb = get_client()
    sb.table("analysis_jobs").update(
        {"status": "failed", "completed_at": _now(), "error": error[:2000]}
    ).eq("id", job_id).execute()


def replace_sheets(version_id: str, rows: list[dict]) -> None:
    """Idempotent: clear any prior sheets for this version, then insert."""
    sb = get_client()
    sb.table("template_sheets").delete().eq("template_version_id", version_id).execute()
    if rows:
        sb.table("template_sheets").insert(rows).execute()


# --- Full raw-extraction snapshot (Option B) -------------------------------
# Private bucket, deterministic path per version. Written/read with the
# service-role key, which bypasses RLS — no storage policy needed.

SNAPSHOT_BUCKET = "template-snapshots"


def ensure_snapshot_bucket() -> None:
    sb = get_client()
    try:
        existing = {b.name for b in sb.storage.list_buckets()}
    except Exception:
        existing = set()
    if SNAPSHOT_BUCKET in existing:
        return
    for opts in ({"public": False}, None):
        try:
            if opts is None:
                sb.storage.create_bucket(SNAPSHOT_BUCKET)
            else:
                sb.storage.create_bucket(SNAPSHOT_BUCKET, options=opts)
            return
        except Exception:
            continue


def _snapshot_path(version_id: str) -> str:
    return f"{version_id}.json.gz"


def upload_snapshot(version_id: str, gz_bytes: bytes) -> str:
    ensure_snapshot_bucket()
    sb = get_client()
    path = _snapshot_path(version_id)
    store = sb.storage.from_(SNAPSHOT_BUCKET)
    opts = {"content-type": "application/gzip", "upsert": "true"}
    try:
        store.upload(path, gz_bytes, opts)
    except Exception:
        # Path exists and upsert wasn't honoured — replace it.
        try:
            store.remove([path])
        except Exception:
            pass
        store.upload(path, gz_bytes, {"content-type": "application/gzip"})
    return path


def download_snapshot(version_id: str) -> bytes:
    sb = get_client()
    return sb.storage.from_(SNAPSHOT_BUCKET).download(_snapshot_path(version_id))


# --- Layer 2 structure persistence ----------------------------------------

def get_sheet_id_map(version_id: str) -> dict[str, str]:
    """sheet name → template_sheets.id for the latest parse of this version."""
    sb = get_client()
    res = (
        sb.table("template_sheets")
        .select("id, name")
        .eq("template_version_id", version_id)
        .execute()
    )
    return {r["name"]: r["id"] for r in (res.data or [])}


def replace_rows(table: str, version_id: str, rows: list[dict], chunk: int = 500) -> None:
    """Idempotent: clear this version's rows in `table`, then insert.

    Rows are inserted in order in chunks. Callers that rely on a self-FK
    (e.g. template_metric_rows.parent_metric_row_id) must pass rows ordered so
    a parent precedes its children — parents have smaller row numbers, so
    sorting by (sheet_name, row) satisfies this.
    """
    sb = get_client()
    sb.table(table).delete().eq("template_version_id", version_id).execute()
    for i in range(0, len(rows), chunk):
        sb.table(table).insert(rows[i:i + chunk]).execute()


# --- Layer 3 understanding: snippet images + persistence -------------------

SNIPPET_BUCKET = "template-snippets"


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name) or "sheet"


def ensure_snippets_bucket() -> None:
    sb = get_client()
    try:
        existing = {b.name for b in sb.storage.list_buckets()}
    except Exception:
        existing = set()
    if SNIPPET_BUCKET in existing:
        return
    for opts in ({"public": False}, None):
        try:
            if opts is None:
                sb.storage.create_bucket(SNIPPET_BUCKET)
            else:
                sb.storage.create_bucket(SNIPPET_BUCKET, options=opts)
            return
        except Exception:
            continue


def upload_snippet(version_id: str, sheet_name: str, png: bytes) -> str:
    """Store a sheet snippet PNG; returns its storage path (private bucket)."""
    ensure_snippets_bucket()
    sb = get_client()
    path = f"{version_id}/{_sanitize(sheet_name)}.png"
    store = sb.storage.from_(SNIPPET_BUCKET)
    opts = {"content-type": "image/png", "upsert": "true"}
    try:
        store.upload(path, png, opts)
    except Exception:
        try:
            store.remove([path])
        except Exception:
            pass
        store.upload(path, png, {"content-type": "image/png"})
    return path


def signed_snippet_url(path: str, expires_in: int = 3600) -> str | None:
    """Time-limited URL for a private snippet, for the browser to <img>."""
    if not path:
        return None
    sb = get_client()
    try:
        res = sb.storage.from_(SNIPPET_BUCKET).create_signed_url(path, expires_in)
    except Exception:
        return None
    return res.get("signedURL") or res.get("signedurl") or res.get("signed_url")


def upsert_understanding(version_id: str, row: dict) -> None:
    """One workbook-understanding row per version: replace it."""
    sb = get_client()
    sb.table("template_understanding").delete().eq("template_version_id", version_id).execute()
    sb.table("template_understanding").insert({**row, "template_version_id": version_id}).execute()


def upsert_data_model(version_id: str, row: dict) -> None:
    """One data-model summary row per version: replace it."""
    sb = get_client()
    sb.table("template_data_model").delete().eq("template_version_id", version_id).execute()
    sb.table("template_data_model").insert({**row, "template_version_id": version_id}).execute()


# --- Template contract + corrections (template-level, span versions) -------

def get_contract(template_id: str) -> dict | None:
    sb = get_client()
    r = sb.table("template_contract").select("*").eq("template_id", template_id).limit(1).execute().data
    return r[0] if r else None


def upsert_contract(template_id: str, fields: dict) -> dict:
    """Create or update the template's contract row; returns it."""
    sb = get_client()
    existing = get_contract(template_id)
    payload = {**fields, "updated_at": _now()}
    if existing:
        sb.table("template_contract").update(payload).eq("template_id", template_id).execute()
    else:
        sb.table("template_contract").insert({"template_id": template_id, **payload}).execute()
    return get_contract(template_id)


def list_corrections(template_id: str, *, include_superseded: bool = False) -> list[dict]:
    sb = get_client()
    q = sb.table("template_corrections").select("*").eq("template_id", template_id)
    if not include_superseded:
        q = q.eq("superseded", False)
    return q.order("created_at").execute().data or []


def add_correction(template_id: str, row: dict) -> dict:
    sb = get_client()
    res = sb.table("template_corrections").insert({"template_id": template_id, **row}).execute()
    return res.data[0]


def add_corrections(template_id: str, rows: list[dict], chunk: int = 500) -> int:
    """Batch-insert corrections (e.g. an LLM-enrichment pass). Returns count."""
    if not rows:
        return 0
    sb = get_client()
    payload = [{"template_id": template_id, **r} for r in rows]
    for i in range(0, len(payload), chunk):
        sb.table("template_corrections").insert(payload[i:i + chunk]).execute()
    return len(payload)


def supersede_corrections_by(template_id: str, created_by: str) -> None:
    """Supersede all corrections from a given author (e.g. re-running enrichment)."""
    sb = get_client()
    sb.table("template_corrections").update({"superseded": True}).eq(
        "template_id", template_id).eq("created_by", created_by).eq("superseded", False).execute()


def supersede_correction(correction_id: str) -> None:
    sb = get_client()
    sb.table("template_corrections").update({"superseded": True}).eq("id", correction_id).execute()
