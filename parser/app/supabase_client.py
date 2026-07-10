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


def fail_stale_jobs(max_age_minutes: int = 180) -> int:
    """Close orphaned 'running' jobs. Jobs execute inside a synchronous HTTP
    request, so a server restart mid-run leaves the row at status='running'
    forever; this reconciles them at startup. Returns how many were closed."""
    from datetime import timedelta
    sb = get_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    res = (
        sb.table("analysis_jobs")
        .update({"status": "failed", "completed_at": _now(),
                 "error": "orphaned: server restarted while the job was running"})
        .eq("status", "running").lt("started_at", cutoff)
        .execute()
    )
    return len(res.data or [])


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


def _ensure_bucket(name: str) -> None:
    """Create a private bucket if it doesn't exist. Creation failure is logged
    (not raised) — the subsequent upload surfaces the real error with context."""
    sb = get_client()
    try:
        existing = {b.name for b in sb.storage.list_buckets()}
    except Exception as e:  # noqa: BLE001 — some keys can't list; upload may still work
        logger.warning("Could not list storage buckets (%s); assuming '%s' exists", e, name)
        return
    if name in existing:
        return
    last_err: Exception | None = None
    for opts in ({"public": False}, None):  # options kwarg varies by client version
        try:
            if opts is None:
                sb.storage.create_bucket(name)
            else:
                sb.storage.create_bucket(name, options=opts)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
    logger.warning("Could not create storage bucket '%s': %s", name, last_err)


def _is_duplicate_error(e: Exception) -> bool:
    """True only for 'object already exists' storage conflicts — auth, missing
    bucket, and network errors must propagate, not trigger remove-and-retry."""
    msg = str(e).lower()
    status = str(getattr(e, "status", None) or getattr(e, "status_code", "") or "")
    return "duplicate" in msg or "already exists" in msg or "409" in status or "409" in msg


def _upload_with_replace(bucket: str, path: str, data: bytes, content_type: str) -> str:
    """Upload with upsert; if the backend still reports a duplicate (older
    storage servers ignore the upsert flag), remove and re-upload."""
    store = get_client().storage.from_(bucket)
    try:
        store.upload(path, data, {"content-type": content_type, "upsert": "true"})
    except Exception as e:
        if not _is_duplicate_error(e):
            raise
        try:
            store.remove([path])
        except Exception:  # noqa: BLE001 — the retry upload surfaces the real failure
            pass
        store.upload(path, data, {"content-type": content_type})
    return path


def ensure_snapshot_bucket() -> None:
    _ensure_bucket(SNAPSHOT_BUCKET)


def _snapshot_path(version_id: str) -> str:
    return f"{version_id}.json.gz"


def upload_snapshot(version_id: str, gz_bytes: bytes) -> str:
    ensure_snapshot_bucket()
    return _upload_with_replace(
        SNAPSHOT_BUCKET, _snapshot_path(version_id), gz_bytes, "application/gzip"
    )


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
    _ensure_bucket(SNIPPET_BUCKET)


def upload_snippet(version_id: str, sheet_name: str, png: bytes) -> str:
    """Store a sheet snippet PNG; returns its storage path (private bucket)."""
    ensure_snippets_bucket()
    path = f"{version_id}/{_sanitize(sheet_name)}.png"
    return _upload_with_replace(SNIPPET_BUCKET, path, png, "image/png")


def signed_snippet_url(path: str, expires_in: int = 3600) -> str | None:
    """Time-limited URL for a private snippet, for the browser to <img>."""
    return _signed_url(SNIPPET_BUCKET, path, expires_in)


def _signed_url(bucket: str, path: str, expires_in: int) -> str | None:
    if not path:
        return None
    try:
        res = get_client().storage.from_(bucket).create_signed_url(path, expires_in)
    except Exception:
        return None
    return res.get("signedURL") or res.get("signedurl") or res.get("signed_url")


# --- Filled workbooks (population output) ----------------------------------

FILLED_BUCKET = "template-filled"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _safe_label(label: str) -> str:
    """A storage-safe slug from an arbitrary source filename/label."""
    base = (label or "source").rsplit(".", 1)[0]  # drop extension
    slug = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base)[:80]
    return slug.strip("_") or "source"


def upload_filled(target_version_id: str, source_label: str, data: bytes) -> str:
    """Store a populated workbook; returns its storage path (private bucket).
    ``source_label`` is an arbitrary source filename — slugged into a safe key."""
    _ensure_bucket(FILLED_BUCKET)
    path = f"{target_version_id}/{_safe_label(source_label)}.xlsx"
    return _upload_with_replace(FILLED_BUCKET, path, data, _XLSX)


def upload_audit(target_version_id: str, source_label: str, data: bytes) -> str:
    """Store a population run's JSON audit alongside its filled workbook; returns
    the storage path (same private bucket)."""
    path = f"{target_version_id}/{_safe_label(source_label)}.audit.json"
    return _upload_with_replace(FILLED_BUCKET, path, data, "application/json")


def signed_filled_url(path: str, expires_in: int = 3600) -> str | None:
    return _signed_url(FILLED_BUCKET, path, expires_in)


def _replace_one_guarded(table: str, version_id: str, row: dict) -> None:
    """Replace a version's single row in an understanding table. These rows are
    expensive LLM output (Opus) and not re-derivable from local state, so
    delete-then-insert carries a compensating guard: on insert failure, restore
    the previous rows (best-effort) before re-raising. Cheaply re-derivable
    tables (replace_rows/replace_sheets) deliberately don't get this."""
    sb = get_client()
    prior = (
        sb.table(table).select("*").eq("template_version_id", version_id).execute().data or []
    )
    sb.table(table).delete().eq("template_version_id", version_id).execute()
    try:
        sb.table(table).insert({**row, "template_version_id": version_id}).execute()
    except Exception:
        if prior:
            try:
                sb.table(table).insert(prior).execute()
                logger.error(
                    "Insert into %s failed for version %s — previous row restored",
                    table, version_id,
                )
            except Exception as restore_err:  # noqa: BLE001
                logger.critical(
                    "Insert into %s failed for version %s AND restoring the previous "
                    "row failed (%s) — expensive understanding data lost",
                    table, version_id, restore_err,
                )
        raise


def upsert_understanding(version_id: str, row: dict) -> None:
    """One workbook-understanding row per version: replace it."""
    _replace_one_guarded("template_understanding", version_id, row)


def upsert_data_model(version_id: str, row: dict) -> None:
    """One data-model summary row per version: replace it."""
    _replace_one_guarded("template_data_model", version_id, row)


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


# --- Authoring: extensible regions (0008) -----------------------------------

def replace_extensible_regions(version_id: str, rows: list[dict]) -> None:
    """Idempotent: clear this version's extensible regions, then insert — same
    delete-then-insert pattern as replace_rows (cheaply re-derivable, so no
    compensating restore). Pre-migration-0010 databases lack the slots columns:
    the insert is retried once with the new keys stripped (logged loudly) so an
    un-migrated environment degrades to legacy region shape instead of failing
    the whole understand run."""
    sb = get_client()
    sb.table("template_extensible_regions").delete().eq(
        "template_version_id", version_id).execute()
    if not rows:
        return
    try:
        sb.table("template_extensible_regions").insert(rows).execute()
    except Exception as e:  # noqa: BLE001 — likely missing 0010 columns
        new_0010 = ("slots", "detection_source", "section_ref")
        stripped = [{k: v for k, v in r.items() if k not in new_0010} for r in rows]
        logger.warning(
            "extensible-region insert failed (%s) — retrying WITHOUT the migration-0010 "
            "columns (slots/detection_source/section_ref). Apply "
            "supabase/migrations/0010_region_slots.sql to enable slot-aware regions.", e)
        sb.table("template_extensible_regions").insert(stripped).execute()


def list_extensible_regions(version_id: str) -> list[dict]:
    sb = get_client()
    res = (
        sb.table("template_extensible_regions")
        .select("*")
        .eq("template_version_id", version_id)
        .order("sheet_name")
        .order("row_start")
        .execute()
    )
    return res.data or []


# --- Review items (0009) -----------------------------------------------------
# Add-only by design: rows carry human answers (and verifier verdicts) that
# must survive re-runs of the understanding, so unlike replace_rows there is
# NO delete here — new questions are inserted, existing ones (matched by
# item_key) are left untouched.

def insert_review_items(version_id: str, rows: list[dict], chunk: int = 200) -> int:
    """Insert only rows whose item_key doesn't already exist for this version.
    Never deletes or overwrites. Returns how many rows were inserted."""
    if not rows:
        return 0
    sb = get_client()
    existing = (
        sb.table("template_review_items")
        .select("item_key")
        .eq("template_version_id", version_id)
        .execute()
        .data
        or []
    )
    seen = {r["item_key"] for r in existing}
    fresh: list[dict] = []
    for r in rows:
        key = r.get("item_key")
        if not key or key in seen:
            continue
        seen.add(key)  # also dedupes repeats within this batch
        fresh.append({**r, "template_version_id": version_id})
    for i in range(0, len(fresh), chunk):
        sb.table("template_review_items").insert(fresh[i:i + chunk]).execute()
    return len(fresh)


def list_review_items(version_id: str) -> list[dict]:
    """All review items for a version — open ones first, then by created_at."""
    sb = get_client()
    res = (
        sb.table("template_review_items")
        .select("*")
        .eq("template_version_id", version_id)
        .order("created_at")
        .execute()
    )
    items = res.data or []
    items.sort(key=lambda r: (0 if r.get("status") == "open" else 1, r.get("created_at") or ""))
    return items


def get_review_item(item_id: str) -> dict | None:
    sb = get_client()
    r = sb.table("template_review_items").select("*").eq("id", item_id).limit(1).execute().data
    return r[0] if r else None


def update_review_item(item_id: str, fields: dict) -> dict:
    sb = get_client()
    sb.table("template_review_items").update(fields).eq("id", item_id).execute()
    return get_review_item(item_id)
