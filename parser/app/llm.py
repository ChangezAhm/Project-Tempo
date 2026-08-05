"""Claude client for Project Tempo — the ONLY module that talks to the Anthropic SDK.

Every model call routes through ``guarded_stream``: one choke point that owns the
spend firewall, model tiers, thinking policy, sampling temperature, truncation
surfacing, and LangSmith tracing (per-call site names + run metadata). Call sites
must not hand-roll any of this — a bypass is a firewall gap and an untraceable
call.

Tracing activates only when LANGSMITH_TRACING=true and LANGSMITH_API_KEY is set
(see parser/.env). If tracing is requested but the langsmith package is missing,
startup logs LOUDLY — silent loss of observability is how blind spots happen.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from functools import lru_cache

import anthropic

logger = logging.getLogger(__name__)

# Model routing by task difficulty (not everything needs Opus):
#   SMART — genuine reasoning over layout/meaning (onboarding "understanding").
#   MAP   — text-only semantic planning/mapping (template metric -> source series).
# Override via env without code changes.
MODEL_SMART = os.environ.get("TEMPO_MODEL_SMART", "claude-opus-4-8")
MODEL_MAP = os.environ.get("TEMPO_MODEL_MAP", "claude-sonnet-4-6")

# Run-scoped metadata attached to every traced call (template_id, run kind …).
# ContextVar does NOT cross threads — worker pools must re-bind it (they already
# re-bind the spend guard; use ``bind_worker`` as the pool initializer).
_LLM_CONTEXT: ContextVar[dict | None] = ContextVar("tempo_llm_context", default=None)


def set_llm_context(ctx: dict | None) -> None:
    _LLM_CONTEXT.set(dict(ctx) if ctx else None)


def get_llm_context() -> dict | None:
    return _LLM_CONTEXT.get()


def bind_worker(guard, ctx: dict | None) -> None:
    """ThreadPoolExecutor initializer: re-bind the spend guard AND the trace
    context inside a worker thread (ContextVars don't cross threads)."""
    from app.population.cost import set_guard

    set_guard(guard)
    set_llm_context(ctx)


def estimate_messages(system: str, messages: list[dict]) -> tuple[int, int]:
    """(input_chars, n_images) across message content, for the spend estimate."""
    chars = len(system or "")
    n_images = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
            continue
        for b in c or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "image":
                n_images += 1
            elif b.get("type") == "text":
                chars += len(b.get("text", ""))
    return chars, n_images


def guarded_stream(*, model: str, system: str, content=None, messages: list[dict] | None = None,
                   max_tokens: int, est_input_chars: int | None = None, n_images: int | None = None,
                   thinking: bool | None = None, temperature: float | None = None,
                   site: str | None = None):
    """Single guarded text/vision call: checks the run's spend cap BEFORE sending,
    records real usage after, gates 'adaptive' thinking to the smart tier,
    surfaces truncation, and names the call in LangSmith (``site`` + the run
    context from set_llm_context). Returns (final_message, first_text_block).

    Pass either ``content`` (a single user turn) or ``messages`` (a full
    conversation, e.g. a corrective-retry exchange). ``temperature`` applies only
    when thinking is off (the API requires default temperature with thinking) —
    planners/mappers pin 0 for run-to-run stability."""
    from app.population.cost import estimate_call_usd, get_guard  # local: avoid import cycle

    if messages is None:
        messages = [{"role": "user", "content": content}]
    if est_input_chars is None or n_images is None:
        chars, imgs = estimate_messages(system, messages)
        est_input_chars = chars if est_input_chars is None else est_input_chars
        n_images = imgs if n_images is None else n_images

    guard = get_guard()
    if guard is not None:
        guard.check(estimate_call_usd(model, est_input_chars, max_tokens, n_images))
    kwargs: dict = {"model": model, "max_tokens": max_tokens, "system": system,
                    "messages": messages}
    use_thinking = thinking if thinking is not None else (model == MODEL_SMART)
    if use_thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    elif temperature is not None:
        kwargs["temperature"] = temperature

    client, wrapped = get_client_info()
    if wrapped:
        meta = dict(get_llm_context() or {})
        if site:
            meta["site"] = site
        kwargs["langsmith_extra"] = {"name": site or "llm_call", "metadata": meta}
    try:
        with client.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
    except TypeError:
        # Older langsmith wrapper without langsmith_extra support — degrade to an
        # unnamed trace rather than failing the call.
        kwargs.pop("langsmith_extra", None)
        with client.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
    if guard is not None and getattr(msg, "usage", None) is not None:
        guard.record_actual(model, msg.usage.input_tokens, msg.usage.output_tokens)
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(
            f"LLM call truncated at max_tokens={max_tokens}"
            + (f" (site={site})" if site else "") + " — shrink the batch or raise it.")
    return msg, next((b.text for b in msg.content if b.type == "text"), "")


@lru_cache(maxsize=1)
def get_client_info() -> tuple[anthropic.Anthropic, bool]:
    """(client, is_langsmith_wrapped). Reads ANTHROPIC_API_KEY from the
    environment (loaded from parser/.env by app.config)."""
    base = anthropic.Anthropic()
    try:
        from langsmith.wrappers import wrap_anthropic

        return wrap_anthropic(base), True
    except Exception as e:  # pragma: no cover - langsmith optional
        if os.environ.get("LANGSMITH_TRACING", "").lower() == "true":
            logger.error(
                "LANGSMITH_TRACING=true but tracing could NOT be enabled (%s) — "
                "LLM calls are running UNTRACED. Install/repair the langsmith package.", e)
        else:
            logger.info("LangSmith tracing not active (%s); using raw Anthropic client", e)
        return base, False


def get_client() -> anthropic.Anthropic:
    return get_client_info()[0]
