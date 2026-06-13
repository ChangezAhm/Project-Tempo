"""Claude client for Layer 3, traced via LangSmith.

Project Tempo's LLM layer is a controlled pipeline of Anthropic-SDK calls
(forced-schema extraction, vision, prompt caching) — not an autonomous agent
loop — so we trace it with LangSmith's `wrap_anthropic` over the Anthropic SDK
(and `@traceable` on the pipeline functions), NOT the Claude Agent SDK.

Tracing activates only when LANGSMITH_TRACING=true and LANGSMITH_API_KEY is set
(see parser/.env). If LangSmith isn't installed/configured the client still
works — wrapping is a no-op.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import anthropic

logger = logging.getLogger(__name__)

# Decision (docs/Layer3-Design.md): every Layer-3 stage runs on Opus 4.8.
MODEL = "claude-opus-4-8"


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    """Anthropic client, LangSmith-wrapped when available.

    Reads ANTHROPIC_API_KEY from the environment (loaded from parser/.env by
    app.config). Raises at first use if the key is missing.
    """
    base = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env
    try:
        from langsmith.wrappers import wrap_anthropic

        return wrap_anthropic(base)
    except Exception as e:  # pragma: no cover - langsmith optional
        logger.info("LangSmith tracing not active (%s); using raw Anthropic client", e)
        return base
