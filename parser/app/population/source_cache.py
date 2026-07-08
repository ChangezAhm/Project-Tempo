"""Tiny content-addressed cache for source understanding.

Understanding a source costs a (small) LLM spend; the same file dropped twice —
during testing, or a re-run — should be free. Keyed by the SHA-256 of the source
bytes, stored as JSON under the system temp dir. Local-only and best-effort; a
production deployment would move this to Supabase storage, but for dev it makes
repeated runs on the same file cost nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR = Path(tempfile.gettempdir()) / "tempo-source-understanding"


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get(key: str) -> dict | None:
    p = _DIR / f"{key}.json"
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.info("source-understanding cache read miss (%s)", e)
    return None


def put(key: str, value: dict) -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        (_DIR / f"{key}.json").write_text(json.dumps(value), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.info("source-understanding cache write skipped (%s)", e)
