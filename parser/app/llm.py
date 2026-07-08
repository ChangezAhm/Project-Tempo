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

# Model routing by task difficulty (not everything needs Opus):
#   SMART — genuine reasoning over layout/meaning (onboarding "understanding").
#   MAP   — text-only semantic mapping (template metric -> source series); cheap.
#   ROUTE — trivial classification (which source sheet); cheapest.
# Override any of them via env without code changes.
import os  # noqa: E402

MODEL_SMART = os.environ.get("TEMPO_MODEL_SMART", "claude-opus-4-8")
MODEL_MAP = os.environ.get("TEMPO_MODEL_MAP", "claude-sonnet-4-6")
MODEL_ROUTE = os.environ.get("TEMPO_MODEL_ROUTE", "claude-haiku-4-5-20251001")

# Back-compat: existing call sites import MODEL. Keep it pointing at the smart
# tier so nothing silently changes behaviour until each site is migrated.
MODEL = MODEL_SMART


def guarded_stream(*, model: str, system: str, content, max_tokens: int,
                   est_input_chars: int, n_images: int = 0):
    """Single guarded text/vision call: checks the run's spend cap BEFORE sending,
    records real usage after, gates 'adaptive' thinking to the smart tier, and
    surfaces truncation. Returns (final_message, first_text_block). Every LLM call
    should route through here so the firewall has no gaps."""
    from app.population.cost import estimate_call_usd, get_guard  # local: avoid import cycle

    guard = get_guard()
    if guard is not None:
        guard.check(estimate_call_usd(model, est_input_chars, max_tokens, n_images))
    kwargs: dict = {"model": model, "max_tokens": max_tokens, "system": system,
                    "messages": [{"role": "user", "content": content}]}
    if model == MODEL_SMART:
        kwargs["thinking"] = {"type": "adaptive"}
    with get_client().messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    if guard is not None and getattr(msg, "usage", None) is not None:
        guard.record_actual(model, msg.usage.input_tokens, msg.usage.output_tokens)
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"LLM call truncated at max_tokens={max_tokens} — shrink the batch or raise it.")
    return msg, next((b.text for b in msg.content if b.type == "text"), "")


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
