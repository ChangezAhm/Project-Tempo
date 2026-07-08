"""AI detection of EXTENSIBLE REGIONS — where a template invites additions.

Part 2A of the Population-Authoring plan: population today can only fill cells
that already exist; it has no concept of the places a template *invites* the
filler to ADD line items (a KPI list with blank slots, an "Other adjustments"
block, "(specify)" rows). Real templates signal these areas in ways a rigid
detector misses, so — mirroring population/source_understanding.py — each
candidate sheet is sent as one compact TEXT digest to one guarded Sonnet call,
and the model returns only STRUCTURE (addresses + row ranges), never values.

"AI owns meaning, code owns facts": every claim is then verified
deterministically against the snapshot — the label column comes from the A1
address, value columns resolve their period dates from the real header cells,
and a region that claims rows whose label cells are already occupied is DROPPED
(the model must not hand population an overwrite). ``total_row`` is stored as a
hard guard: the subtotal already SUMs across the blank rows and must never be
written. Results persist per version in template_extensible_regions (0008) —
the authoring half of the Template Contract.

Snapshot loading REUSES app.datamodel.derive._load_snapshot (imported, not
re-implemented): it already handles the missing-snapshot fallback (re-parse the
workbook), and region detection must read the exact same cell truth the data
model was derived from.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, ConfigDict

from app import supabase_client as sb
from app.llm import MODEL_MAP, guarded_stream
from app.population.catalogue import effective_value
from app.population.cost import SpendCapExceeded, SpendGuard, default_cap_usd, set_guard
from app.population.periods import parse_any_date
from app.raw_extraction.column_utils import column_index

logger = logging.getLogger(__name__)


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RegionOut(_M):
    kind: str = "other"                    # kpi_list | other_adjustments | custom_rows | other
    label_col_cell: str = ""               # A1 address in the label column of the first free row
    row_start: int = 0                     # first row available for additions
    row_end: int = 0                       # last row available
    total_row: int | None = None           # subtotal row — never written
    value_header_cells: list[str] = []     # period header cells whose columns new lines must fill
    rules: str | None = None               # author guidance ("enter one KPI per row", units)
    confidence: float = 0.0
    evidence: list[str] = []               # cell refs the model cited


class RegionsOut(_M):
    regions: list[RegionOut] = []


_SYSTEM = (
    "You read ONE sheet of a financial TEMPLATE and locate its EXTENSIBLE REGIONS — "
    "places the template invites the filler to ADD line items:\n"
    "- blank repeating rows under a section, with the same column shape as the filled "
    "rows above (a list with empty slots),\n"
    '- "(specify)" / "Other…" / "Add KPI" style labels,\n'
    "- dropdown data validations on label cells,\n"
    "- a subtotal row whose SUM range already spans the blank rows.\n"
    "You report STRUCTURE only — never values. Every address and row number you output "
    "MUST come from the TEXT DIGEST (it is authoritative). For each region give:\n"
    "- kind: kpi_list | other_adjustments | custom_rows | other,\n"
    "- label_col_cell: an A1 address IN THE LABEL COLUMN of the FIRST FREE row (e.g. "
    '"B31"),\n'
    "- row_start / row_end: the contiguous BLANK rows available for additions — never "
    "include a row whose label cell already has text, and never include the total row,\n"
    "- total_row: the subtotal row that must never be written (null if none),\n"
    "- value_header_cells: the period/value HEADER cells whose columns each new line "
    'must fill (e.g. ["E10","F10"]),\n'
    '- rules: short author guidance for whoever adds a line ("enter one KPI per row", '
    "units, sign),\n"
    "- confidence in [0,1] and evidence: the cell refs that convinced you.\n"
    "Be conservative: only clear invitations. A merely-empty area with no repeating "
    "shape, no inviting label, no validation and no spanning subtotal is NOT a region — "
    'return {"regions":[]} when nothing qualifies.\n'
    'Return ONLY JSON: {"regions":[{"kind":"...","label_col_cell":"B31","row_start":31,'
    '"row_end":38,"total_row":39,"value_header_cells":["E10","F10"],"rules":"...",'
    '"confidence":0.9,"evidence":["B25","B39"]}]}'
)

_MAX_TOKENS = 8000       # a sheet has at most a handful of regions
_HEADER_ROWS = 20
_MAX_BODY_ROWS = 300     # bounds the digest so one big sheet can't overflow
_MAX_BLANK_RUNS = 60
_MAX_VALIDATIONS = 30
_FILLABLE = ("data", "sourced")   # fact categories population writes into (see datamodel)
_KINDS = {"kpi_list", "other_adjustments", "custom_rows", "other"}

_CELL = re.compile(r"^([A-Z]+)(\d+)$")


def _rc(addr: str | None) -> tuple[int, int] | None:
    """'AD31' -> (col=30, row=31), 1-based col. None for anything that isn't A1."""
    m = _CELL.match((addr or "").strip().upper())
    return (column_index(m.group(1)), int(m.group(2))) if m else None


def _blank(v) -> bool:
    return v is None or v == ""


def _col_letters(addr: str) -> str:
    m = _CELL.match((addr or "").strip().upper())
    return m.group(1) if m else ""


def _runs(rows: list[int]) -> list[tuple[int, int]]:
    """Consecutive row numbers folded into (start, end) runs."""
    out: list[list[int]] = []
    for r in sorted(set(rows)):
        if out and r == out[-1][1] + 1:
            out[-1][1] = r
        else:
            out.append([r, r])
    return [(a, b) for a, b in out]


# --- Digest ------------------------------------------------------------------

def _digest(sheet: dict) -> str:
    """Compact text view for the model: a header band, one line per labelled row
    (with ROW NUMBER, label-cell address, filled + blank value columns), the runs
    of blank-but-formatted rows (the snapshot keeps an empty cell only when the
    author styled it as an input / unlocked it — exactly the "invitation" signal),
    and the sheet's data validations. Addresses are authoritative; sizes capped."""
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
            if _blank(v):
                continue
            parts.append(f"{c.get('address')}={str(v)[:24]}")
        if parts:
            lines.append("  " + " ".join(parts[:40]))

    lines.append("ROWS (row | label addr='label' | filled cols | blank-formatted cols):")
    blank_only: dict[int, list[str]] = {}   # fully blank rows, for the runs section
    body = 0
    for r in sorted(by_row):
        rcs = sorted(by_row[r], key=lambda c: c["col"])
        filled = [c for c in rcs if not _blank(effective_value(c))]
        blanks = [c for c in rcs if _blank(effective_value(c))]
        if not filled:
            if blanks:
                blank_only[r] = [c.get("address", "") for c in blanks]
            continue
        if r <= _HEADER_ROWS:
            continue   # already shown in the header band
        label = next((c for c in filled
                      if isinstance(effective_value(c), str)
                      and str(effective_value(c)).strip()
                      and not str(effective_value(c)).startswith("=")), None)
        lab = (f"{label.get('address')}='{str(effective_value(label)).strip()[:48]}'"
               if label is not None else "(no text label)")
        line = f"  {r} | {lab}"
        others = [c for c in filled if c is not label]
        if others:
            line += " | filled: " + " ".join(c.get("address", "") for c in others[:8])
        if blanks:
            line += " | blank: " + " ".join(c.get("address", "") for c in blanks[:8])
        lines.append(line)
        body += 1
        if body >= _MAX_BODY_ROWS:
            lines.append("  ...(more rows truncated)")
            break

    if blank_only:
        lines.append("BLANK-BUT-FORMATTED ROW RUNS (empty cells styled as inputs — add slots?):")
        for a, b in _runs(list(blank_only))[:_MAX_BLANK_RUNS]:
            letters = sorted({_col_letters(addr)
                              for r in range(a, b + 1) for addr in blank_only.get(r, [])},
                             key=column_index)
            span = f"rows {a}-{b}" if b > a else f"row {a}"
            lines.append(f"  {span} | cols {','.join(letters)}")

    dvs = sheet.get("data_validations") or []
    if dvs:
        lines.append("DATA VALIDATIONS (range | type | allowed | prompt):")
        for v in dvs[:_MAX_VALIDATIONS]:
            allowed = ", ".join(str(a) for a in (v.get("allowed_values") or [])[:12])
            line = f"  {v.get('cell_range')} | {v.get('validation_type')}"
            if allowed:
                line += f" | allowed: {allowed}"
            prompt = (v.get("prompt_message") or "").strip()
            if prompt:
                line += f" | prompt: {prompt[:80]}"
            lines.append(line)
    return "\n".join(lines)


# --- LLM call + parse --------------------------------------------------------

def _parse(text: str) -> RegionsOut:
    """Parse the model's JSON (code fences tolerated). Raises on no/invalid JSON
    so the caller can run the corrective retry; {"regions":[]} is a valid answer."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("reply contained no JSON object")
    return RegionsOut(**json.loads(t[a:b + 1]))


def _detect(digest: str, model: str) -> RegionsOut:
    """One guarded Sonnet call for one sheet, with ONE corrective retry when the
    reply doesn't parse (mirrors population/mapping.py). Raises after the retry
    fails — the caller decides whether that skips the sheet."""
    _, text = guarded_stream(model=model, system=_SYSTEM, content=digest,
                             max_tokens=_MAX_TOKENS,
                             est_input_chars=len(_SYSTEM) + len(digest))
    try:
        return _parse(text)
    except Exception as e:  # noqa: BLE001 — malformed JSON from the model
        err = str(e)
    logger.warning("regions reply didn't parse (%s) — one corrective retry", err)
    messages = [
        {"role": "user", "content": digest},
        {"role": "assistant", "content": text[:4000]},
        {"role": "user", "content": (
            f"That reply was not usable ({err}). Return ONLY the JSON object "
            '{"regions":[...]} for the sheet above — no prose, no fences.'
        )},
    ]
    _, text = guarded_stream(model=model, system=_SYSTEM, messages=messages,
                             max_tokens=_MAX_TOKENS)
    return _parse(text)


# --- Deterministic conversion / verification ---------------------------------

def _cell_map(sheet: dict) -> dict[tuple[int, int], dict]:
    """(row, col) -> cell for verification lookups."""
    return {(c["row"], c["col"]): c for c in sheet.get("cells", [])}


def _convert(r: RegionOut, sheet_name: str,
             cmap: dict[tuple[int, int], dict]) -> tuple[dict | None, str | None]:
    """Turn one model claim into a DB row, verifying it against the snapshot.
    Returns (row, None) or (None, skip_reason). Code owns the facts here: the
    label column comes from the A1 address, period dates from the real header
    cells, and any claim over occupied label cells is rejected."""
    where = f"{sheet_name}!{r.label_col_cell or '?'}"
    rc = _rc(r.label_col_cell)
    if rc is None:
        return None, f"{where}: label_col_cell {r.label_col_cell!r} is not an A1 address"
    label_col = rc[0]

    row_start, row_end = int(r.row_start), int(r.row_end)
    if row_end - row_start + 1 < 1:
        return None, f"{where}: capacity < 1 (rows {row_start}..{row_end})"
    if r.total_row is not None and row_start <= int(r.total_row) <= row_end:
        return None, f"{where}: total_row {r.total_row} lies inside the add range {row_start}..{row_end}"

    occupied = []
    for row in range(row_start, row_end + 1):
        cell = cmap.get((row, label_col))
        if cell is not None and not _blank(effective_value(cell)):
            occupied.append(row)
    if occupied:
        return None, (f"{where}: label cells already occupied at rows {occupied[:5]} "
                      "— the model claimed non-empty rows")

    value_cols: list[dict] = []
    seen: set[int] = set()
    for addr in r.value_header_cells:
        hc = _rc(addr)
        if hc is None or hc[0] in seen:
            continue
        seen.add(hc[0])
        cell = cmap.get((hc[1], hc[0]))
        d = parse_any_date(effective_value(cell)) if cell is not None else None
        value_cols.append({"col": hc[0], "parsed_date": d.isoformat() if d else None})
    value_cols.sort(key=lambda v: v["col"])

    kind = (r.kind or "other").strip().lower()
    return {
        "sheet_name": sheet_name,
        "kind": kind if kind in _KINDS else "other",
        "label_col": label_col,
        "value_cols": value_cols,
        "row_start": row_start,
        "row_end": row_end,
        "total_row": int(r.total_row) if r.total_row is not None else None,
        "rules": r.rules,
        "confidence": max(0.0, min(1.0, float(r.confidence))),
        "evidence": list(r.evidence)[:20],
    }, None


def detect_sheet_regions(sheet: dict, *, model: str = MODEL_MAP) -> tuple[list[dict], list[str]]:
    """One sheet end-to-end: digest → guarded call (one retry) → verified rows.
    Returns (rows_without_version_stamp, skip_reasons)."""
    out = _detect(_digest(sheet), model)
    cmap = _cell_map(sheet)
    name = sheet.get("name") or ""
    rows, skipped = [], []
    for r in out.regions:
        row, reason = _convert(r, name, cmap)
        if row is not None:
            rows.append(row)
        else:
            skipped.append(reason)
    return rows, skipped


# --- Entry points -------------------------------------------------------------

def detect_and_persist(template_id: str) -> dict:
    """Detect extensible regions for the template's latest version and persist
    them (idempotent replace). Populate-tier spend guard — this is cheap Sonnet
    text, one call per data sheet. Candidate sheets are those with fillable
    (data/sourced) facts in the data model: a sheet population can't write has
    nothing to extend. A single sheet that errors is skipped (not fatal); a
    spend-cap breach still aborts the run."""
    from app.datamodel.derive import _load_snapshot   # lazy: pulls the Aspose parse chain
    from app.datamodel.persist import get_data_model

    set_guard(SpendGuard(default_cap_usd()))
    try:
        dm = get_data_model(template_id, limit=30000)
        if not dm.get("available"):
            raise RuntimeError("No data model for this template yet — derive it first.")
        version_id = dm["template_version_id"]
        fillable_sheets = {f.get("sheet_name") for f in (dm.get("facts") or [])
                           if f.get("category") in _FILLABLE}

        snapshot = _load_snapshot(version_id, template_id)
        rows: list[dict] = []
        skipped: list[str] = []
        for sheet in snapshot.get("sheets", []):
            name = sheet.get("name")
            if name not in fillable_sheets:
                continue
            try:
                srows, sskip = detect_sheet_regions(sheet)
            except SpendCapExceeded:
                raise
            except Exception as e:  # noqa: BLE001 — one odd sheet can't sink the run
                logger.exception("region detection failed for sheet %s — skipping", name)
                skipped.append(f"{name}: detection failed ({e})")
                continue
            rows.extend(srows)
            skipped.extend(sskip)

        payload = [{**r, "template_version_id": version_id} for r in rows]
        sb.replace_extensible_regions(version_id, payload)
        logger.info("extensible regions: %d persisted, %d skipped for version %s",
                    len(payload), len(skipped), version_id)
        return {"template_version_id": version_id, "regions": payload,
                "count": len(payload), "skipped": skipped}
    finally:
        set_guard(None)


def get_regions(template_id: str) -> dict:
    """Read back the stored regions for the template's latest version."""
    version_id, _, _ = sb.get_latest_file(template_id)
    regions = sb.list_extensible_regions(version_id)
    return {"template_version_id": version_id, "count": len(regions), "regions": regions}
