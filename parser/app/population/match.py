"""LLM matching: read the SOURCE workbook and locate each metric the template
needs. Output is a mapping (source row/col + transforms) — NEVER values. The
template's data model is the query; the source is the corpus.
"""

from __future__ import annotations

import json
import logging

from app.llm import MODEL, get_client
from app.population.schema import PopulationMapping
from app.understanding.per_sheet import _extract_json, to_strict_schema
from app.understanding.sheet_image import render_sheet_tiles
from app.understanding.sheet_view import build_text_grid

logger = logging.getLogger(__name__)
_SCHEMA = to_strict_schema(PopulationMapping)

SYSTEM = (
    "You map a portfolio company's SOURCE workbook to a reporting template's required inputs. "
    "You are given the template's DEMAND (the metrics it needs; the number of relative monthly "
    "periods, period_index 0..N-1 where the HIGHEST index is the most recent / the as-of month; "
    "and the scenarios) and ONE source sheet (text grid with exact addresses + image).\n"
    "For each demand metric you can find on THIS source sheet, output a metric_match: the source "
    "row holding that metric's values, the verbatim source_label, unit_scale (multiply source→"
    "template units, e.g. source in thousands but template in millions → 0.001; same units → 1.0), "
    "sign_flip if sign conventions differ, the scenario the source REPRESENTS (a management-"
    "accounts / actuals file is 'actual' even if no budget is shown alongside; use null only if "
    "genuinely scenario-agnostic), and confidence.\n"
    "Also output period_aligns: for each source column that holds period data, which template "
    "period_index it maps to — align by DATE (the source's period dates vs the template's N months "
    "ending at the as-of date).\n"
    "CRITICAL: never output any values or numbers — only LOCATIONS (rows/columns) and transforms. "
    "Cite real source cell addresses. Omit a metric entirely if it is not on this sheet (it may be "
    "on another). List demand metrics you are confident are absent from the whole source in "
    "unmatched_metrics only on the last sheet."
)


def _build_user(demand: dict, grid: str) -> str:
    return (
        f"## TEMPLATE DEMAND\n{json.dumps(demand)}\n\n"
        f"## SOURCE SHEET (grid)\n{grid}\n\n"
        "## OUTPUT\nReturn ONLY a JSON object matching this schema:\n" + json.dumps(_SCHEMA)
    )


def _call(user_text: str, images: list[tuple[str, bytes]], max_tokens: int = 16000):
    import base64
    content: list[dict] = []
    for cap, png in images or []:
        if cap:
            content.append({"type": "text", "text": cap})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": base64.standard_b64encode(png).decode("ascii")}})
    content.append({"type": "text", "text": user_text})
    with get_client().messages.stream(model=MODEL, max_tokens=max_tokens, thinking={"type": "adaptive"},
                                      system=SYSTEM, messages=[{"role": "user", "content": content}]) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"Matching truncated at max_tokens={max_tokens} — raise it.")
    return msg, next((b.text for b in msg.content if b.type == "text"), "")


def match_sheet(demand: dict, source_sheet: dict, images: list[tuple[str, bytes]]) -> PopulationMapping:
    user = _build_user(demand, build_text_grid(source_sheet))
    msg, text = _call(user, images)
    try:
        return PopulationMapping.model_validate(json.loads(_extract_json(text)))
    except Exception as e:  # one corrective retry
        logger.warning("match parse failed for %s (%s); retrying", source_sheet.get("name"), e)
        msg, text = _call(user + f"\n\nThat did not parse ({e}). Return ONLY the corrected JSON.", images)
        return PopulationMapping.model_validate(json.loads(_extract_json(text)))


def build_mapping(demand: dict, source_snapshot: dict, source_workbook_path) -> PopulationMapping:
    """Run matching across all source sheets and aggregate one mapping."""
    metric_matches: list = []
    period_aligns: list = []
    unmatched: list[str] = []
    notes: list[str] = []
    for sheet in source_snapshot.get("sheets", []):
        if not sheet.get("cells"):
            continue
        try:
            imgs = render_sheet_tiles(source_workbook_path, sheet["name"])
        except Exception as e:  # noqa: BLE001
            logger.warning("source render failed for %s (%s) — text-only", sheet["name"], e)
            imgs = []
        m = match_sheet(demand, sheet, imgs)
        metric_matches.extend(m.metric_matches)
        period_aligns.extend(m.period_aligns)
        unmatched.extend(m.unmatched_metrics)
        notes.extend(m.notes)
    # a metric flagged unmatched on one sheet but matched on another is actually matched
    matched_names = {mm.template_metric for mm in metric_matches}
    unmatched = sorted({u for u in unmatched if u not in matched_names})
    return PopulationMapping(metric_matches=metric_matches, period_aligns=period_aligns,
                             unmatched_metrics=unmatched, notes=notes)
