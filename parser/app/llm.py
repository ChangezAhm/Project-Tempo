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

try:
    from langsmith import traceable as _traceable
except ImportError:  # pragma: no cover - langsmith optional
    _traceable = None

logger = logging.getLogger(__name__)

if _traceable is None and os.environ.get("LANGSMITH_TRACING", "").lower() == "true":
    logger.error("LANGSMITH_TRACING=true but the langsmith package is missing — "
                 "LLM calls are running UNTRACED. Install/repair langsmith.")

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
                   site: str | None = None, cache_blocks: int = 0):
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
    sys_payload = system
    if cache_blocks > 0:
        # PROMPT CACHING: a stable prefix (system + the first ``cache_blocks``
        # content blocks — the workbook grids) is cached server-side, so calls
        # that repeat it (mapping batches, retries, the revision pass) pay ~10%
        # for it instead of full price. A big template once burned $20 re-
        # sending identical grids seven times. Callers keep the STABLE part in
        # the leading blocks and the per-call part after.
        sys_payload = [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}]
        first = messages[0]
        if isinstance(first.get("content"), list) and len(first["content"]) >= cache_blocks:
            marked = [dict(b) for b in first["content"]]
            marked[cache_blocks - 1] = {**marked[cache_blocks - 1],
                                        "cache_control": {"type": "ephemeral"}}
            messages = [{**first, "content": marked}, *messages[1:]]
    kwargs: dict = {"model": model, "max_tokens": max_tokens, "system": sys_payload,
                    "messages": messages}
    use_thinking = thinking if thinking is not None else (model == MODEL_SMART)
    if use_thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    elif temperature is not None:
        kwargs["temperature"] = temperature

    client = get_client()

    def _invoke(prompt=None):  # ``prompt`` exists so the TRACE shows the real prompt
        with client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    # Explicit LLM-level trace. wrap_anthropic does not hook the .stream()
    # context-manager path, so without this the AI call never appeared as its
    # own run — only the outer @traceable function with raw args. This records
    # a named child run whose Input IS the prompt (image tiles replaced by
    # placeholders so the payload stays under LangSmith's cap and renders fast).
    if _traceable is not None and os.environ.get("LANGSMITH_TRACING", "").lower() == "true":
        meta = dict(get_llm_context() or {})
        meta["model"] = model
        if site:
            meta["site"] = site
        traced = _traceable(run_type="llm", name=site or "llm_call")(_invoke)
        msg = traced(_display_prompt(system, messages), langsmith_extra={"metadata": meta})
    else:
        msg = _invoke()
    if guard is not None and getattr(msg, "usage", None) is not None:
        u = msg.usage
        # cache tokens bill at 1.25x (write) / 0.1x (read) of the input rate —
        # fold them into an EFFECTIVE input-token count so the guard tracks
        # real spend, not just uncached input.
        eff_in = (u.input_tokens
                  + int((getattr(u, "cache_creation_input_tokens", 0) or 0) * 1.25)
                  + int((getattr(u, "cache_read_input_tokens", 0) or 0) * 0.1))
        guard.record_actual(model, eff_in, u.output_tokens)
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(
            f"LLM call truncated at max_tokens={max_tokens}"
            + (f" (site={site})" if site else "") + " — shrink the batch or raise it.")
    return msg, next((b.text for b in msg.content if b.type == "text"), "")


def _display_prompt(system: str, messages: list[dict]) -> list[dict]:
    """The prompt as recorded in the trace: verbatim text, but image tiles
    replaced by small placeholders (megabytes of base64 would blow LangSmith's
    payload cap and hide the whole input)."""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        c = m.get("content")
        if not isinstance(c, list):
            out.append(m)
            continue
        blocks = []
        for b in c:
            if isinstance(b, dict) and b.get("type") == "image":
                kb = len(((b.get("source") or {}).get("data") or "")) // 1024
                blocks.append({"type": "text", "text": f"[image tile omitted — ~{kb} KB base64]"})
            else:
                blocks.append(b)
        out.append({**m, "content": blocks})
    return out


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    """Raw Anthropic client (ANTHROPIC_API_KEY from env, loaded from parser/.env
    by app.config). Tracing is done explicitly in guarded_stream — the
    wrap_anthropic client wrapper is NOT used (it misses .stream() calls)."""
    return anthropic.Anthropic()
