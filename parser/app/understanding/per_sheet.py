"""Per-sheet understanding agent: image + grid → grounded structured output.

Calls Opus 4.8 with the SheetUnderstanding schema enforced (structured outputs),
adaptive thinking on, streamed (large outputs). Traced in LangSmith via the
wrapped client + @traceable. Validates that cited cells are real.
"""

from __future__ import annotations

import base64
import json
import logging
import re

from langsmith import traceable

from app.llm import MODEL, get_client
from app.understanding.prompts import SYSTEM, build_user_text
from app.understanding.schema import SheetUnderstanding

logger = logging.getLogger(__name__)

# A single cell ("D41") or a range ("D10:O10") — input fields may use either.
_ADDR_RE = re.compile(r"^[A-Z]{1,3}\d+(?::[A-Z]{1,3}\d+)?$")

# Anthropic's vision API rejects images over 5 MB. Large sheets render bigger
# than that at 150 dpi; rather than have the call 400 (and lose the sheet), send
# those text-only — the grid carries the exact addresses the image only hints at.
_MAX_IMAGE_BYTES = 5_000_000

# Schema-node keywords structured outputs doesn't support (stripped).
_UNSUPPORTED = (
    "title", "default",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "minItems", "maxItems", "pattern", "format",
)
# Keys whose VALUE is a {name: subschema} map — recurse into values, never treat
# the names as keywords (a property may legitimately be named "title").
_SCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
# Keys whose value is a list of subschemas.
_SCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")


def to_strict_schema(model: type) -> dict:
    """Pydantic model → Claude-structured-outputs-compatible JSON schema.

    Schema-aware: forces additionalProperties:false + required=all-props on every
    object and strips unsupported annotation keywords — but only on actual schema
    nodes, never on property names. (Our models have no defaults, so every field
    is already required/nullable.)
    """
    schema = model.model_json_schema()

    def walk(node: dict) -> None:
        for k in _UNSUPPORTED:
            node.pop(k, None)
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for key in _SCHEMA_MAPS:
            sub = node.get(key)
            if isinstance(sub, dict):
                for child in sub.values():
                    if isinstance(child, dict):
                        walk(child)
        for key in _SCHEMA_LISTS:
            sub = node.get(key)
            if isinstance(sub, list):
                for child in sub:
                    if isinstance(child, dict):
                        walk(child)
        items = node.get("items")
        if isinstance(items, dict):
            walk(items)
        elif isinstance(items, list):
            for child in items:
                if isinstance(child, dict):
                    walk(child)

    walk(schema)
    return schema


_SCHEMA = to_strict_schema(SheetUnderstanding)


def _grounding_report(result: SheetUnderstanding, populated: set[str]) -> dict:
    """Content citations should reference populated cells; input cells are often
    empty (awaiting entry), so those are only format-checked."""
    content_cited = 0
    content_unmatched: list[str] = []

    def content(addr: str | None):
        nonlocal content_cited
        if not addr:
            return
        content_cited += 1
        if addr not in populated:
            content_unmatched.append(addr)

    for s in result.sections:
        for e in s.evidence:
            content(e)
    for m in result.metric_rows:
        content(m.label_cell)
        for e in m.evidence:
            content(e)
    for p in result.periods:
        content(p.cell)
    for r in result.author_rules:
        content(r.source_cell)

    input_cells = [c for f in result.input_fields for c in f.cells]
    bad_format = [c for c in input_cells if not _ADDR_RE.match(c)]

    by_source: dict[str, int] = {}
    for m in result.metric_rows:
        if m.interpretation_source:
            s = m.interpretation_source.value
            by_source[s] = by_source.get(s, 0) + 1

    return {
        "content_citations": content_cited,
        "content_unmatched": len(content_unmatched),
        "content_unmatched_sample": content_unmatched[:10],
        "input_cells": len(input_cells),
        "input_cells_bad_format": len(bad_format),
        "metrics_with_definition": sum(1 for m in result.metric_rows if m.definition),
        "interpretation_by_source": by_source,
    }


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json(text: str) -> str:
    """Pull the JSON object out of a model reply (strip ``` fences / prose)."""
    t = _FENCE_RE.sub("", text).strip()
    start, end = t.find("{"), t.rfind("}")
    return t[start : end + 1] if start != -1 and end != -1 else t


def _call(client, messages, max_tokens: int, sheet_name: str):
    # Schema enforced by prompt + Pydantic validation (not output_config) — the
    # strict-grammar compiler rejects schemas this large. Adaptive thinking stays
    # on (a forced tool_choice would disable it).
    with client.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},   # effort defaults to high on Opus 4.8
        system=SYSTEM,
        messages=messages,
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Understanding truncated at max_tokens={max_tokens} for '{sheet_name}' — raise max_tokens."
        )
    text = next((b.text for b in msg.content if b.type == "text"), "")
    return msg, text


@traceable(name="understand_sheet", run_type="chain")
def understand_sheet(
    sheet: dict,
    images: list[tuple[str, bytes]],
    annotations: str,
    workbook_ctx: str,
    hints: str,
    *,
    max_tokens: int = 32000,
) -> dict:
    """Run the per-sheet agent. Returns {understanding, grounding, usage}.

    ``images`` is a list of (caption, png) tiles — one for a normal sheet, a few
    column-band slices for a wide one, or empty when the sheet couldn't be
    rendered legibly. With no images the agent works from the text grid alone,
    which still carries the exact cell addresses.
    """
    from app.understanding.sheet_view import build_text_grid

    grid = build_text_grid(sheet)
    schema_note = (
        "\n\n## OUTPUT\nReturn ONLY a single JSON object (no prose, no code fences) "
        "matching this JSON schema exactly:\n" + json.dumps(_SCHEMA)
    )
    body = build_user_text(grid, annotations, workbook_ctx, hints) + schema_note

    # Final guard: keep only tiles within Anthropic's per-image size cap (the
    # renderer already fits dimensions; this catches anything that slipped).
    imgs = [(cap, png) for (cap, png) in (images or []) if png and len(png) <= _MAX_IMAGE_BYTES]

    content: list[dict] = []
    if imgs:
        if len(imgs) > 1:
            content.append({"type": "text", "text": (
                f"This sheet is shown as {len(imgs)} horizontal slices; the leftmost label "
                "columns are repeated in each slice. Together they are ONE sheet — don't "
                "double-count the repeated label columns."
            )})
        for cap, png in imgs:
            if cap:
                content.append({"type": "text", "text": cap})
            b64 = base64.standard_b64encode(png).decode("ascii")
            content.append(
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}
            )
    else:
        body = (
            "NOTE: no rendered image of this sheet is available — rely on the text grid below, "
            "which carries the exact cell addresses.\n\n" + body
        )
    content.append({"type": "text", "text": body})

    client = get_client()
    messages = [{"role": "user", "content": content}]

    msg, text = _call(client, messages, max_tokens, sheet["name"])
    try:
        result = SheetUnderstanding.model_validate(json.loads(_extract_json(text)))
    except Exception as e:  # one corrective retry
        logger.warning("First understanding parse failed for %s (%s); retrying", sheet["name"], e)
        messages += [
            {"role": "assistant", "content": text[:4000]},
            {"role": "user", "content": (
                f"That did not parse as valid SheetUnderstanding JSON: {e}. "
                "Return ONLY the corrected JSON object — no prose, no code fences."
            )},
        ]
        msg, text = _call(client, messages, max_tokens, sheet["name"])
        result = SheetUnderstanding.model_validate(json.loads(_extract_json(text)))

    populated = {c["address"] for c in sheet.get("cells", [])}
    grounding = _grounding_report(result, populated)

    return {
        "understanding": result,
        "grounding": grounding,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
    }
