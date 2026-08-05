"""AI understanding of a SOURCE workbook (text digest + sheet image, cheap, cached).

Real PortCo files vary too much for a deterministic detector — periods are often
formula-driven (=array_timeline), labels live anywhere, units are implicit. So we
let an LLM read the sheet and tell us the STRUCTURE only:
  - which columns are periods (with their dates/grain),
  - which rows are data series (label cell, metric, unit, currency, sign).

Each selected sheet is sent as a cached-value TEXT DIGEST (authoritative for
addresses) plus a few rendered PNG tiles (Aspose) so the model sees the layout a
digest flattens — merged period headers, units declared in banners, visual
Actual/Budget blocks. The image aids comprehension only: every address must come
from the digest, and the model never reads or returns numbers — deterministic
code reads the real values from the snapshot at the cells the AI points to
(catalogue_from_understanding). This is the source-side mirror of "AI owns
meaning, code owns facts": Sonnet, a handful of data sheets, under the populate
spend cap (tiles are priced into the pre-flight estimate), and cached by file
content so re-runs are free. Disable images with TEMPO_SOURCE_VISION=0.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.llm import MODEL_MAP, guarded_stream
from app.population import source_cache
from app.population.catalogue import effective_value

logger = logging.getLogger(__name__)


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SrcPeriod(_M):
    header_cell: str = ""          # A1 of the period's header cell, e.g. "AD11"
    date: str | None = None        # ISO date the header resolves to ("2023-01-31")
    grain: str = "month"           # month | quarter | year | ltm | ytd | other
    kind: str = "actual"           # actual | budget | forecast


class SrcSeries(_M):
    label_cell: str = ""           # A1 of the row's label cell, e.g. "B20"
    label: str = ""
    canonical_metric: str | None = None
    unit: str | None = None        # "EUR'm", "%", "x" — as the sheet implies
    currency: str | None = None
    sign_flip: bool = False        # source shows this with the opposite sign to convention
    # Scenario is judged PER ROW by the model (layouts vary too much for rules):
    # which scenario this row carries, and — when the row is a scenario RESTATEMENT
    # of another metric row (a 'Budget' row under Revenue; a budget block repeating
    # the P&L) — the label of that parent metric row.
    scenario: str | None = None    # actual | budget | forecast | null (unclear)
    variant_of: str | None = None  # parent metric row's label, when this row restates it


class SrcSheetOut(_M):
    periods: list[SrcPeriod] = []
    series: list[SrcSeries] = []


_SYSTEM = (
    "You read ONE sheet of a financial SOURCE workbook and report its STRUCTURE so a "
    "deterministic program can extract values. You do NOT report any values.\n"
    "Return JSON with two lists:\n"
    "1) periods: each TIME column's header cell (A1), the date it represents (ISO "
    "YYYY-MM-DD if you can tell, else null), its grain (month/quarter/year/ltm/ytd), "
    "and kind (actual/budget/forecast). Include every monthly column you can see; mark "
    "LTM/YTD/FY summary columns with the right grain so they aren't mistaken for months.\n"
    "2) series: each DATA ROW's label cell (A1), its label, a canonical_metric "
    "(snake_case, e.g. revenue, cost_of_sales, gross_profit, ebitda, net_debt) or null, "
    "its unit (e.g. \"EUR'm\", \"%\", \"x\") and currency if known, and sign_flip=true only "
    "if the row is shown with the opposite sign to the usual convention. Skip header/"
    "section/total-only rows that aren't data.\n"
    "SCENARIO — judge it PER ROW from whatever the sheet actually does (layouts vary: "
    "interleaved 'Budget' rows, budget blocks, side-by-side columns, colour/section "
    "conventions): set scenario to actual/budget/forecast (null if unclear). When a row "
    "RESTATES another metric row under a different scenario — a bare 'Budget' row under "
    "Revenue, a 'Budget (Revenue)' row, a budget block repeating the P&L lines — set "
    "variant_of to the PARENT metric row's label exactly as it appears, so the program "
    "can pair them. A row that is itself the primary statement of its metric has "
    "variant_of=null. When scenario differs BY COLUMN rather than by row, express it "
    "with the periods' kind instead.\n"
    "If rendered image(s) of the sheet are provided, use them ONLY to understand the "
    "LAYOUT — merged period headers, units/currency declared in banners, Actual vs "
    "Budget blocks, which rows are real data vs headings. Every header_cell/label_cell "
    "you output MUST be an address shown in the TEXT DIGEST (it is authoritative); "
    "never take an address or a value from the image.\n"
    'Return ONLY JSON: {"periods":[...],"series":[...]}.'
)

_MAX_TOKENS = 16000      # structured output for a sheet's worth of series/periods
_HEADER_ROWS = 20
_MAX_BODY_ROWS = 400     # bounds output size so one big sheet can't overflow _MAX_TOKENS
_MAX_SAMPLES = 8

# Vision: layout context, not full detail — a couple of tiles per sheet is enough
# and keeps the image spend trivial next to the text.
_MAX_IMAGE_BYTES = 5_000_000   # Anthropic's per-image cap; oversized tiles are dropped
_MAX_TILES = 3                 # per sheet
_EST_TILES_PER_SHEET = 2       # dry-run estimate (tiles aren't rendered for a dry run)
_CACHE_VERSION = 3             # bump when the understanding inputs change materially
                               # (v3: per-series scenario + variant_of) — older entries re-run


def vision_enabled() -> bool:
    return os.environ.get("TEMPO_SOURCE_VISION", "1") != "0"


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def select_sheets(snapshot: dict, *, max_sheets: int = 10, min_numeric: int = 12) -> list[dict]:
    """Data-bearing sheets, by count of numeric (computed) cells, densest first."""
    ranked = []
    for s in snapshot.get("sheets", []):
        n = sum(1 for c in s.get("cells", []) if _is_num(effective_value(c)))
        if n >= min_numeric:
            ranked.append((n, s))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in ranked[:max_sheets]]


def _digest(sheet: dict) -> str:
    """Compact text view: a header band (exposes date headers/titles) + one line per
    labelled data row with a few sample cell addresses (no full value dump)."""
    cells = sheet.get("cells", [])
    by_row: dict[int, list[dict]] = {}
    for c in cells:
        by_row.setdefault(c["row"], []).append(c)

    lines: list[str] = [f"SHEET: {sheet.get('name')}"]
    lines.append("HEADER BAND (addr=value):")
    for r in sorted(k for k in by_row if k <= _HEADER_ROWS):
        parts = []
        for c in sorted(by_row[r], key=lambda c: c["col"]):
            v = effective_value(c)
            if v is None or v == "":
                continue
            parts.append(f"{c.get('address')}={str(v)[:24]}")
        if parts:
            lines.append("  " + " ".join(parts[:40]))

    lines.append("DATA ROWS (label_cell | label | sample addr=value):")
    body = 0
    for r in sorted(k for k in by_row if k > _HEADER_ROWS):
        rcs = sorted(by_row[r], key=lambda c: c["col"])
        label_cell = next((c for c in rcs
                           if isinstance(effective_value(c), str) and str(effective_value(c)).strip()), None)
        nums = [c for c in rcs if _is_num(effective_value(c))]
        if label_cell is None or not nums:
            continue
        samp = " ".join(f"{c.get('address')}={effective_value(c)}" for c in nums[:_MAX_SAMPLES])
        lines.append(f"  {label_cell.get('address')} | {str(effective_value(label_cell)).strip()[:48]} | {samp}")
        body += 1
        if body >= _MAX_BODY_ROWS:
            lines.append(f"  ...(+{len(by_row) - r} more rows truncated)")
            break
    return "\n".join(lines)


def _parse(text: str) -> SrcSheetOut:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1:
        return SrcSheetOut()
    return SrcSheetOut(**json.loads(t[a:b + 1]))


def _build_content(digest: str, tiles: list[tuple[str, bytes]]) -> tuple[list[dict] | str, int]:
    """User content: captioned image tiles first, the text digest last. Returns
    (content, n_images) — plain text when there are no usable tiles. Oversized
    tiles are dropped rather than 400-ing the whole call."""
    imgs = [(cap, png) for (cap, png) in (tiles or []) if png and len(png) <= _MAX_IMAGE_BYTES]
    if not imgs:
        return digest, 0
    content: list[dict] = []
    if len(imgs) > 1:
        content.append({"type": "text", "text": (
            f"This sheet is shown as {len(imgs)} horizontal slices; the leftmost label "
            "columns repeat in each slice. Together they are ONE sheet — don't "
            "double-count the repeated label columns."
        )})
    for cap, png in imgs:
        if cap:
            content.append({"type": "text", "text": cap})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.standard_b64encode(png).decode("ascii")}})
    content.append({"type": "text", "text": digest})
    return content, len(imgs)


def _render_tiles(source_path: str | Path | None, sheet_name: str | None) -> list[tuple[str, bytes]]:
    """Best-effort PNG tiles of one source sheet (Aspose). Empty when vision is
    off, there's no workbook path (snapshot-only callers), or the render fails —
    the digest alone still carries everything binding needs."""
    if source_path is None or not sheet_name or not vision_enabled():
        return []
    try:
        from app.understanding.sheet_image import render_sheet_tiles
        return render_sheet_tiles(source_path, sheet_name, max_tiles=_MAX_TILES)
    except Exception:  # noqa: BLE001 — a failed render must never sink the sheet
        logger.exception("sheet image render failed for %s — proceeding text-only", sheet_name)
        return []


def _understand_sheet(sheet: dict, model: str,
                      tiles: list[tuple[str, bytes]] = ()) -> dict:
    digest = _digest(sheet)
    content, n_images = _build_content(digest, tiles)
    _, text = guarded_stream(model=model, system=_SYSTEM, content=content,
                             max_tokens=_MAX_TOKENS,
                             est_input_chars=len(_SYSTEM) + len(digest),
                             n_images=n_images,
                             site=f"source_understanding:{sheet.get('name')}")
    out = _parse(text)
    return {"sheet": sheet.get("name"),
            "periods": [p.model_dump() for p in out.periods],
            "series": [s.model_dump() for s in out.series]}


def cached_sheets(content_hash: str | None) -> list[dict] | None:
    """The cached understanding for this file, or None on a miss. Entries written
    by an older _CACHE_VERSION are treated as a miss, so an improvement (e.g.
    adding sheet images) actually reaches files understood before it."""
    if not content_hash:
        return None
    cached = source_cache.get(content_hash)
    if cached is None or cached.get("version") != _CACHE_VERSION:
        return None
    return cached.get("sheets", [])


def estimate_source_understanding_usd(snapshot: dict, *, model: str = MODEL_MAP,
                                      max_sheets: int = 8) -> float:
    """What a full (uncached) source-understanding would cost — no LLM call, no
    render (image count is a per-sheet constant estimate)."""
    from app.population.cost import estimate_call_usd
    n_images = _EST_TILES_PER_SHEET if vision_enabled() else 0
    total = 0.0
    for s in select_sheets(snapshot, max_sheets=max_sheets):
        d = _digest(s)
        total += estimate_call_usd(model, len(_SYSTEM) + len(d), _MAX_TOKENS, n_images)
    return round(total, 4)


def understand_source(snapshot: dict, content_hash: str | None = None, *,
                      model: str = MODEL_MAP, max_sheets: int = 8,
                      source_path: str | Path | None = None) -> list[dict]:
    """Return [{sheet, periods, series}] for the source's data sheets. Cached by
    content_hash (free on repeat). Each sheet is one cheap Sonnet call — text
    digest + rendered tiles when ``source_path`` is given — guarded by the run's
    spend cap. A single sheet that errors is skipped (not fatal), so one odd
    sheet can't sink the whole source — but a spend-cap breach still aborts."""
    from app.population.cost import SpendCapExceeded

    cached = cached_sheets(content_hash)
    if cached is not None:
        logger.info("source understanding: cache hit (%s sheets)", len(cached))
        return cached

    sheets, failed = [], []
    for s in select_sheets(snapshot, max_sheets=max_sheets):
        tiles = _render_tiles(source_path, s.get("name"))
        try:
            sheets.append(_understand_sheet(s, model, tiles))
        except SpendCapExceeded:
            raise
        except Exception:
            failed.append(s.get("name"))
            logger.exception("source understanding failed for sheet %s — skipping", s.get("name"))

    # Cache only COMPLETE results. Caching a partial one would make every future
    # populate of this file cache-hit and never retry the failed sheet —
    # permanent silent degradation.
    if content_hash and sheets and not failed:
        source_cache.put(content_hash, {"version": _CACHE_VERSION, "sheets": sheets})
    elif failed:
        logger.warning("source understanding incomplete (failed: %s) — result NOT cached "
                       "so the next run retries", failed)
    return sheets
