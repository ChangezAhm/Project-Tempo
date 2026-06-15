"""Source-search matching (Build A v3).

The template is ALREADY analysed — the data model gives us every input cell with
its metric, period, scenario, unit and definition. So matching does NOT re-read
the template: it hands the model that analysed context as text and asks it to
LOCATE each input in the SOURCE workbook (the only thing it hasn't seen). This
removes the redundant re-rendering and re-sending of the template that made
population slow, and drops population's dependency on the template snapshot.

Stage 1 (route_sheets): template sheet → source sheet(s), cheap + text-only.
Stage 2 (match_source): for one template sheet's inputs, read ONLY the source
(image + grid) and return cell→cell links. Runs in parallel across batches.
"""

from __future__ import annotations

import base64
import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from app.llm import MODEL, get_client
from app.population.schema import CellLink, RoutingOut, SheetMatchOut
from app.understanding.per_sheet import _extract_json, to_strict_schema
from app.understanding.sheet_image import render_sheet_tiles
from app.understanding.sheet_view import build_text_grid

logger = logging.getLogger(__name__)

_ROUTE_SCHEMA = to_strict_schema(RoutingOut)
_MATCH_SCHEMA = to_strict_schema(SheetMatchOut)
_MAX_IMAGE_BYTES = 5_000_000

_CELLS_PER_CALL = 200        # template inputs per match call (bounds output size)
_SRC_SHEETS_SHOWN = 3        # source sheets shown with image+grid per call
_MAX_PARALLEL = 6            # concurrent match calls (independent post-routing; calls are light)


SYSTEM_ROUTE = (
    "You map a reporting TEMPLATE's sheets to a portfolio company's SOURCE workbook sheets. "
    "For each template sheet, list the source sheet name(s) whose data fills it — by financial "
    "MEANING (P&L / income statement, balance sheet, cash flow, debt schedule, KPIs / operational "
    "metrics), not by name spelling. A source sheet may feed several template sheets, and a "
    "template sheet may need several source sheets. Skip source sheets that are clearly irrelevant "
    "(cover, instructions, blank, lookup/config). Use ONLY source sheet names from the provided list."
)

SYSTEM_MATCH = (
    "You locate, in a portfolio company's SOURCE workbook, the values that fill a reporting "
    "template's already-known inputs.\n"
    "The template has ALREADY been analysed — you are GIVEN its inputs as structured context "
    "(cell, metric, period, scenario, unit, definition). You do NOT see the template, and you do "
    "not need to: trust the context. Your only job is to find WHERE each input's value lives in the "
    "SOURCE (shown as image + text grid).\n"
    "For each input you can find, output a link: template_cell (copy it verbatim from the context), "
    "source_sheet, source_cell (exact A1 you can see in the source grid), unit_scale (multiply "
    "source→template units; source in thousands but template in millions → 0.001; same units → 1.0), "
    "sign_flip (true if sign conventions differ, e.g. costs positive in the source but negative in "
    "the template), confidence 0..1, and a short note.\n"
    "Align PERIODS by date: the template periods are relative (p0..pN-1; the as-of date and grain are "
    "given; the highest index is the most recent). Read the source's own date/period headers and map "
    "each template period to the matching source column. Align SCENARIO: an actuals / management-"
    "accounts source supplies 'actual'; only fill a budget or forecast input if the source actually "
    "shows that scenario.\n"
    "If an input's value is genuinely not present in the source, omit it (it is reported unmatched). "
    "If a listed input is obviously not a real metric (a stray label/placeholder), put it in skipped.\n"
    "CRITICAL: output LOCATIONS ONLY — never values or numbers. Cite real source addresses you can "
    "see; never invent a cell."
)


def _call(system: str, user_text: str, images: list[tuple[str, bytes]], max_tokens: int = 16000):
    content: list[dict] = []
    for cap, png in images or []:
        if not png or len(png) > _MAX_IMAGE_BYTES:
            continue
        if cap:
            content.append({"type": "text", "text": cap})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.standard_b64encode(png).decode("ascii")}})
    content.append({"type": "text", "text": user_text})
    with get_client().messages.stream(model=MODEL, max_tokens=max_tokens, thinking={"type": "adaptive"},
                                      system=system, messages=[{"role": "user", "content": content}]) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "max_tokens":
        raise RuntimeError(f"Matching truncated at max_tokens={max_tokens} — raise it or shrink the batch.")
    return msg, next((b.text for b in msg.content if b.type == "text"), "")


def _sheet_labels(sheet: dict, limit: int = 14) -> list[str]:
    """Leftmost text labels of a source sheet — a cheap content fingerprint for routing."""
    out: list[str] = []
    for c in sorted(sheet.get("cells", []), key=lambda c: (c.get("row", 0), c.get("col", 0))):
        if c.get("col", 99) <= 3 and isinstance(c.get("value"), str):
            v = c["value"].strip()
            if v and not v.replace(".", "").replace(",", "").replace("-", "").isdigit():
                out.append(v)
                if len(out) >= limit:
                    break
    return out


def route_sheets(facts_by_sheet: dict[str, list[dict]], src_sheets: dict[str, dict],
                 as_of_date: str | None) -> dict[str, list[str]]:
    """Stage 1: template sheet → source sheet(s). Best-effort; failures fall back
    to 'show all source sheets' downstream."""
    tpl_lines = []
    for tname, tfacts in facts_by_sheet.items():
        labels = []
        for f in tfacts:
            lbl = f.get("metric_label") or f.get("canonical_metric")
            if lbl and lbl not in labels:
                labels.append(lbl)
            if len(labels) >= 8:
                break
        tpl_lines.append(f"- {tname}: inputs e.g. {', '.join(labels)}")
    src_lines = [f"- {sname}: rows e.g. {', '.join(_sheet_labels(s))}" for sname, s in src_sheets.items()]
    user = (
        f"## TEMPLATE SHEETS (need filling)\n" + "\n".join(tpl_lines) + "\n\n"
        f"## SOURCE SHEETS (available)\n" + "\n".join(src_lines) + "\n\n"
        "## OUTPUT\nReturn ONLY a JSON object matching this schema:\n" + json.dumps(_ROUTE_SCHEMA)
    )
    try:
        _, text = _call(SYSTEM_ROUTE, user, [], max_tokens=4000)
        routing = RoutingOut.model_validate(json.loads(_extract_json(text)))
    except Exception as e:  # noqa: BLE001 — routing is advisory
        logger.warning("sheet routing failed (%s) — defaulting each template sheet to all sources", e)
        return {t: list(src_sheets) for t in facts_by_sheet}
    valid = {s.lower(): s for s in src_sheets}
    out: dict[str, list[str]] = {}
    for r in routing.routes:
        picked = [valid[s.lower()] for s in r.source_sheets if s.lower() in valid]
        if picked:
            out[r.template_sheet] = picked
    return out


def _demand_lines(facts: list[dict]) -> str:
    """The analysed template inputs as compact text — the context the model uses
    instead of re-reading the template. One line per input cell."""
    lines = []
    for f in facts:
        lbl = f.get("metric_label") or f.get("canonical_metric") or "?"
        canon = f.get("canonical_metric")
        parts = [str(f.get("cell")), lbl + (f" [{canon}]" if canon and canon != lbl else "")]
        pl = f.get("period_label")
        parts.append(f"p{f.get('period_index')}" + (f" {pl}" if pl else ""))
        parts.append(str(f.get("scenario")))
        if f.get("unit"):
            parts.append(f"unit:{f['unit']}")
        if f.get("definition"):
            parts.append(f"def:{str(f['definition'])[:120]}")
        if f.get("expected_source"):
            parts.append(f"expects:{str(f['expected_source'])[:80]}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def match_source(tname: str, facts: list[dict], routed: list[str], src_sheets: dict[str, dict],
                 src_images: dict, demand: dict) -> SheetMatchOut:
    """Stage 2: locate this template sheet's (already-analysed) inputs in the SOURCE.
    The template is NOT shown — only its analysed context (text) + the source."""
    images: list[tuple[str, bytes]] = []
    for sname in routed[:_SRC_SHEETS_SHOWN]:
        for cap, png in src_images.get(sname, []):
            images.append((f"SOURCE SHEET '{sname}' — {cap}" if cap else f"SOURCE SHEET '{sname}'", png))

    src_grids = "\n\n".join(f"### SOURCE: {s}\n{build_text_grid(src_sheets[s])}"
                            for s in routed if s in src_sheets)
    pc = demand.get("period_count") or 0
    user = (
        f"## CONTEXT\n"
        f"The template expects {pc} {demand.get('period_grain') or 'monthly'} periods "
        f"(p0..p{max(pc - 1, 0)}); as-of date: "
        f"{demand.get('as_of_date') or 'unknown — treat the latest source period as the most recent'}. "
        f"Scenarios in scope: {demand.get('scenarios') or ['actual']}.\n\n"
        f"## TEMPLATE INPUTS TO FIND  (sheet '{tname}'; already analysed — locate each in the SOURCE)\n"
        f"format: cell | metric | period | scenario | [unit] | [def] | [expects]\n"
        f"{_demand_lines(facts)}\n\n"
        f"## SOURCE SHEETS\n{src_grids}\n\n"
        f"## OUTPUT\nReturn ONLY a JSON object matching this schema:\n" + json.dumps(_MATCH_SCHEMA)
    )
    try:
        _, text = _call(SYSTEM_MATCH, user, images)
        return SheetMatchOut.model_validate(json.loads(_extract_json(text)))
    except Exception as e:  # one corrective retry
        logger.warning("match parse failed for template sheet %s (%s); retrying", tname, e)
        _, text = _call(SYSTEM_MATCH, user + f"\n\nThat did not parse ({e}). Return ONLY the corrected JSON.", images)
        return SheetMatchOut.model_validate(json.loads(_extract_json(text)))


def build_links(facts: list[dict], source_snapshot: dict, source_workbook_path,
                demand: dict) -> tuple[list[CellLink], list[dict], dict, list[str]]:
    """Route, then locate each template sheet's analysed inputs in the source. The
    template is never re-read — only the source is rendered/sent. Match calls run
    CONCURRENTLY. Returns (links, skipped, routing, notes)."""
    src_sheets = {s["name"]: s for s in source_snapshot.get("sheets", []) if s.get("cells")}

    by_sheet: dict[str, list[dict]] = defaultdict(list)
    for f in facts:
        by_sheet[f["sheet_name"]].append(f)
    for tname in by_sheet:
        by_sheet[tname].sort(key=lambda f: (f.get("row") or 0, f.get("col") or 0))

    routing = route_sheets(by_sheet, src_sheets, demand.get("as_of_date"))

    notes: list[str] = []
    src_img_cache: dict[str, list] = {}

    def _tiles(name):
        if name not in src_img_cache:
            try:
                src_img_cache[name] = render_sheet_tiles(source_workbook_path, name)
            except Exception as e:  # noqa: BLE001 — text grid still carries addresses
                logger.warning("source render failed for %s (%s) — text-only", name, e)
                src_img_cache[name] = []
        return src_img_cache[name]

    tasks: list[tuple[str, list[str], list[dict], int]] = []
    for tname, tfacts in by_sheet.items():
        routed = [s for s in (routing.get(tname) or list(src_sheets)) if s in src_sheets][:_SRC_SHEETS_SHOWN + 2]
        if not routed:
            notes.append(f"template sheet '{tname}': no source sheet routed — {len(tfacts)} inputs unmatched")
            continue
        for s in routed:
            _tiles(s)
        for i in range(0, len(tfacts), _CELLS_PER_CALL):
            tasks.append((tname, routed, tfacts[i:i + _CELLS_PER_CALL], i))

    def _run(task):
        tname, routed, batch, offset = task
        try:
            return tname, match_source(tname, batch, routed, src_sheets, src_img_cache, demand), None
        except Exception as e:  # noqa: BLE001 — one bad batch shouldn't kill the run
            logger.warning("match batch failed for %s [%d:] (%s)", tname, offset, e)
            return tname, None, f"template sheet '{tname}' batch at {offset}: match failed ({e})"

    links: list[CellLink] = []
    skipped: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, max(1, len(tasks)))) as ex:
        results = list(ex.map(_run, tasks))

    for tname, out, err in results:
        if err:
            notes.append(err)
            continue
        for lk in out.links:
            links.append(CellLink(template_sheet=tname, template_cell=lk.template_cell,
                                  source_sheet=lk.source_sheet, source_cell=lk.source_cell,
                                  unit_scale=lk.unit_scale, sign_flip=lk.sign_flip,
                                  confidence=lk.confidence, note=lk.note))
        for sk in out.skipped:
            skipped.append({"template_sheet": tname, "template_cell": sk.template_cell, "reason": sk.reason})
        notes.extend(out.notes)

    return links, skipped, routing, notes
