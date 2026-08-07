"""Live populate progress — the real stage, not a guess.

_run_population stamps its current stage here; GET /populate-progress/{id}
serves it so the add-in pane shows what the engine is actually doing. One run
per template at a time (main._single_run), so template_id is the key.
In-process only by design: progress is ephemeral UX, never state.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_runs: dict[str, dict] = {}

# Canonical stage order the pane renders; "reading" happens client-side.
STAGES = ("understanding", "planning", "verifying", "writing")


def set_stage(template_id: str, stage: str, detail: str | None = None) -> None:
    with _lock:
        _runs[template_id] = {"stage": stage, "detail": detail, "at": time.time()}


def get_stage(template_id: str) -> dict | None:
    with _lock:
        cur = _runs.get(template_id)
        return dict(cur) if cur else None


def clear(template_id: str) -> None:
    with _lock:
        _runs.pop(template_id, None)
