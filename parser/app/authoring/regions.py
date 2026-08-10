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
from app.population.cost import SpendCapExceeded, SpendGuard, default_onboarding_cap_usd, set_guard
from app.population.periods import parse_any_date
from app.priors import ADJUSTMENT_MEMBER_STRICT, ADJUSTMENT_SUBTOTAL, is_placeholder_slot_label
from app.raw_extraction.column_utils import column_index, column_letter

logger = logging.getLogger(__name__)


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SlotOut(_M):
    """One row of a region with an explicit WRITE MODE — the unit of safety.
    blank: append target. placeholder: throwaway label that may be overwritten
    (approval-gated). editable_label: author-marked-editable real label
    (approval-gated, structural evidence required)."""
    row: int = 0
    mode: str = "blank"              # blank | placeholder | editable_label
    current_label: str | None = None # verbatim label text (trust anchor for overwrites)
    evidence: list[str] = []


class RegionOut(_M):
    kind: str = "other"                    # see _KINDS
    label_col_cell: str = ""               # A1 address in the label column of the first free row
    row_start: int = 0                     # first row available for additions
    row_end: int = 0                       # last row available
    total_row: int | None = None           # subtotal row — never written
    value_header_cells: list[str] = []     # period header cells whose columns new lines must fill
    rules: str | None = None               # author guidance ("enter one KPI per row", units)
    slots: list[SlotOut] = []              # per-row modes; [] => every row in range is blank
    confidence: float = 0.0
    evidence: list[str] = []               # cell refs the model cited


class RegionsOut(_M):
    regions: list[RegionOut] = []


# --- placeholder / signal detection (deterministic) ---------------------------
# is_placeholder_slot_label (imported from app.priors — the loose tier, bare
# "Other"/"New" included) corroborates a model 'placeholder' claim — text
# judgment alone never makes a real label overwritable.


def _validation_cols_rows(sheet: dict) -> set[tuple[int, int]]:
    """(row, col) pairs covered by any data validation on this sheet."""
    out: set[tuple[int, int]] = set()
    for v in sheet.get("data_validations") or []:
        for sub in str(v.get("cell_range") or "").split(","):
            parts = sub.replace("$", "").strip().split(":")
            a = _rc(parts[0])
            b = _rc(parts[1]) if len(parts) > 1 else a
            if not a or not b:
                continue
            for row in range(min(a[1], b[1]), max(a[1], b[1]) + 1):
                for col in range(min(a[0], b[0]), max(a[0], b[0]) + 1):
                    out.add((row, col))
    return out


# --- Roll-up (spanning-subtotal) blocks: deterministic adjustment detection ----
#
# The single fact that proves a block of occupied labels is an EXTENSIBLE LIST is
# a subtotal that SUMs a contiguous range of those rows (an EBITDA bridge is
# exactly this: Restructuring / Transaction Costs / … -> Adjusted EBITDA). The
# snapshot keeps that formula but the text digest renders only its cached number,
# so the LLM never sees the roll-up and treats the lines as fixed. We recover the
# fact deterministically — to annotate the digest, to corroborate an editable
# claim (summed_member signal), and to guarantee a region the LLM misses.

_SUM_RANGE = re.compile(
    r"(?:SUM|SUBTOTAL)\s*\(\s*(?:\d+\s*,\s*)?\$?([A-Z]+)\$?(\d+)\s*:\s*\$?([A-Z]+)\$?(\d+)\s*\)",
    re.I)
# subtotal-label (ADJUSTMENT_SUBTOTAL) and member-label (ADJUSTMENT_MEMBER_STRICT)
# detection comes from app.priors — the shared adjustment lexicon (strict tier:
# member matching can make rows editable, so bare 'transaction' must not fire).


def _formula(cell: dict | None) -> str | None:
    """A cell's formula text — from `formula` (real Aspose parse) or, failing that,
    from `value` when it is an '=…' string (older/test cells keep it there)."""
    if not cell:
        return None
    f = cell.get("formula")
    if f:
        return str(f)
    v = cell.get("value")
    return v if isinstance(v, str) and v.startswith("=") else None


def _text_label(cell: dict | None) -> str | None:
    """The cell's business label — the DISPLAYED string, formula-driven or not.
    A formula label (`=_PL!AB20`, `=val_company&" P&L Accounts"`) still labels
    the row; skipping it once made every formula-labelled roll-up block
    invisible to the deterministic detectors (no summed_member signal, no
    safety net) on connector templates."""
    if cell is None:
        return None
    v = effective_value(cell)   # cached display value for formula cells
    if isinstance(v, str):
        t = v.strip()
        if t and not t.startswith("="):
            return t
    return None


def _subtotal_blocks(sheet: dict) -> list[dict]:
    """Deterministic roll-up blocks: a subtotal cell whose formula SUMs a
    CONTIGUOUS vertical range of LABELLED rows just above it. One entry per
    (range, label_col): {lo, hi, subtotal_row, label_col, value_cols,
    member_rows, labelled_rows, subtotal_label, member_labels}. Requires a real
    labelled list (>=2 rows named, majority named) so blank add-slot runs — which
    the blank-run detection already handles — don't qualify here."""
    cmap = _cell_map(sheet)
    by_key: dict[tuple[int, int, int], dict] = {}
    for (row, col), cell in cmap.items():
        f = _formula(cell)
        if not f:
            continue
        for m in _SUM_RANGE.finditer(f):
            c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
            vcol = column_index(c1)
            if vcol != column_index(c2):
                continue                          # single-column vertical sums only
            lo, hi = min(r1, r2), max(r1, r2)
            if hi - lo + 1 < 2 or not (hi < row <= hi + 3):
                continue                          # >=2 rows, subtotal sits just below
            # the label column: the leftmost text-label column across member rows
            label_cols: dict[int, int] = {}
            for rr in range(lo, hi + 1):
                for cc in range(1, vcol):         # labels sit left of the value column
                    if _text_label(cmap.get((rr, cc))):
                        label_cols[cc] = label_cols.get(cc, 0) + 1
                        break
            if not label_cols:
                continue
            label_col = min(label_cols, key=lambda k: (-label_cols[k], k))
            key = (lo, hi, label_col)
            blk = by_key.get(key)
            if blk is None:
                labelled = [rr for rr in range(lo, hi + 1)
                            if _text_label(cmap.get((rr, label_col)))]
                blk = by_key[key] = {
                    "lo": lo, "hi": hi, "subtotal_row": row, "label_col": label_col,
                    "value_cols": [], "member_rows": list(range(lo, hi + 1)),
                    "labelled_rows": labelled,
                    "subtotal_label": _text_label(cmap.get((row, label_col))),
                    "member_labels": [_text_label(cmap.get((rr, label_col))) for rr in labelled],
                }
            if vcol not in blk["value_cols"]:
                blk["value_cols"].append(vcol)
    out: list[dict] = []
    for blk in by_key.values():
        n, nl = len(blk["member_rows"]), len(blk["labelled_rows"])
        if nl >= 2 and nl >= 0.5 * n:
            blk["value_cols"].sort()
            out.append(blk)
    return out


def _is_adjustment_block(block: dict, sheet: dict) -> bool:
    """Does a roll-up block read as an EARNINGS-ADJUSTMENT list (an EBITDA bridge,
    a normalisation / one-off block) rather than a fixed-statement subtotal (Gross
    Profit, Total Assets)? Deterministic lexicon over the subtotal label, the
    member labels, and the section header just above — so the safety net fires on
    adjustment blocks and stays off ordinary statement subtotals."""
    if ADJUSTMENT_SUBTOTAL.search(block.get("subtotal_label") or ""):
        return True
    if any(lab and ADJUSTMENT_MEMBER_STRICT.search(lab) for lab in block.get("member_labels") or []):
        return True
    cmap = _cell_map(sheet)
    lc = block["label_col"]
    for rr in (block["lo"] - 1, block["lo"] - 2):
        hdr = _text_label(cmap.get((rr, lc)))
        if hdr and (ADJUSTMENT_SUBTOTAL.search(hdr) or re.search(r"(?i)\b(bridge|adjustment)", hdr)):
            return True
    return False


def _label_signals(sheet: dict) -> dict[tuple[int, int], list[str]]:
    """Structural editability signals per cell: unlocked (on a protected sheet —
    the author explicitly freed it), input-style fill, validated (a dropdown on a
    label cell is an author invitation), placeholder-text, and summed_member (the
    row is summed by a spanning subtotal — structurally part of an aggregated
    list). These are the deterministic corroboration for occupied-row slot
    claims."""
    from app.raw_extraction.cell_analyzer import is_input_fill
    protected = bool(sheet.get("is_protected"))
    validated = _validation_cols_rows(sheet)
    out: dict[tuple[int, int], list[str]] = {}
    for c in sheet.get("cells", []):
        sigs: list[str] = []
        st = c.get("style") or {}
        if protected and st.get("is_locked") is False:
            sigs.append("unlocked")
        if is_input_fill(st.get("fill_color")):
            sigs.append("input_fill")
        if (c.get("row"), c.get("col")) in validated:
            sigs.append("validated")
        v = effective_value(c)
        if isinstance(v, str) and is_placeholder_slot_label(v):
            sigs.append("placeholder_text")
        if sigs:
            out[(c["row"], c["col"])] = sigs
    # roll-up membership: an occupied label the filler may rename/extend because a
    # spanning subtotal already sums its row.
    for blk in _subtotal_blocks(sheet):
        lc = blk["label_col"]
        for rr in blk["labelled_rows"]:
            out.setdefault((rr, lc), []).append("summed_member")
    return out


_SYSTEM = (
    "You read ONE sheet of a financial TEMPLATE and locate its EXTENSIBLE / EDITABLE "
    "REGIONS — the places the filler may ADD line items or CHANGE existing labels:\n"
    "- blank repeating rows under a section, with the same column shape as the filled "
    "rows above (a list with empty slots),\n"
    '- PLACEHOLDER rows: throwaway pre-printed labels the filler replaces — "Custom '
    'KPI 1", "[Specify]", "Other…", "Adjustment 3",\n'
    "- EDITABLE label rows the author marked changeable (unlocked or validated label "
    "cells — see LABEL-CELL SIGNALS): named EBITDA adjustment lines, like-for-like "
    "adjustment labels, renamable titles, an own chart of accounts,\n"
    "- dropdown data validations on label cells,\n"
    "- a subtotal row whose SUM range already spans the rows,\n"
    "- a CONFIGURABLE METRIC LIST: a KPI dashboard, scorecard, operational-metrics, or "
    "custom-metrics section where the ROW LABELS name metrics the filler CHOOSES or "
    "DEFINES (not the fixed line items of a financial statement). Flag EVERY metric row "
    "of such a section as an editable_label slot (kind=kpi_list) EVEN WITHOUT a per-cell "
    "dropdown/unlock signal — the section is configurable BY DESIGN. This is a SEMANTIC "
    "judgment: a KPI/scorecard/operational area is configurable; the standard lines of a "
    "P&L, Balance Sheet or Cash Flow (Revenue, COGS, Cash, Debt, EBITDA) are FIXED.\n"
    "- an ADJUSTMENT / BRIDGE block: rows summed by a subtotal into an 'Adjusted / "
    "Normalised / Pro-forma' figure — an EBITDA bridge (Restructuring, One-off items, "
    "Share-based comp … -> Adjusted EBITDA). A subtotal tagged [ROLL-UP sums rows a-b] "
    "and rows tagged [summed_member] MARK exactly this shape: the member rows are an "
    "extensible list the filler adds to, so flag them as editable_label slots "
    "(kind=adjustment_rows). The [ROLL-UP]/[summed_member] tags are authoritative "
    "structural facts — trust them over a row looking pre-printed.\n"
    "You report STRUCTURE only — never values. Every address and row number MUST come "
    "from the TEXT DIGEST (it is authoritative). For each region give:\n"
    "- kind: kpi_list | custom_rows | adjustment_rows | editable_labels | "
    "chart_of_accounts | other,\n"
    "- label_col_cell: an A1 address IN THE LABEL COLUMN of the first slot row,\n"
    "- row_start / row_end: the region's row span (slots say what each row is) — "
    "never include the total row,\n"
    "- total_row: the subtotal row that must never be written (null if none),\n"
    "- value_header_cells: the period/value HEADER cells whose columns each line "
    'must fill (e.g. ["E10","F10"]),\n'
    "- slots: ONE ENTRY PER ROW: {row, mode, current_label}. mode='blank' for an empty "
    "row (append target); mode='placeholder' for a throwaway label (copy the label "
    "VERBATIM into current_label); mode='editable_label' for a real-looking label the "
    "author marked editable (only when LABEL-CELL SIGNALS shows unlocked/validated for "
    "that cell — cite it in evidence). Occupied rows are allowed ONLY as placeholder/"
    "editable_label slots with their current_label recorded.\n"
    '- rules: short author guidance ("enter one KPI per row", units, sign),\n'
    "- confidence in [0,1] and evidence: the cell refs that convinced you.\n"
    "Be conservative: only clear invitations. A merely-empty area with no repeating "
    "shape, no inviting label, no validation and no spanning subtotal is NOT a region — "
    'return {"regions":[]} when nothing qualifies. On a FIXED financial statement '
    "(P&L, Balance Sheet, Cash Flow) a real line-item label with NO structural signal "
    "is NOT editable — never claim it. The ONLY exception is a CONFIGURABLE METRIC LIST "
    "(KPI dashboard / scorecard / custom-metrics section, kind=kpi_list): there the "
    "metric rows ARE editable labels by design, so claim them even without a per-cell "
    "signal — but you must be confident the section is configurable, not a fixed "
    "statement.\n"
    'Return ONLY JSON: {"regions":[{"kind":"...","label_col_cell":"B31","row_start":31,'
    '"row_end":38,"total_row":39,"value_header_cells":["E10","F10"],'
    '"slots":[{"row":31,"mode":"blank","current_label":null}],"rules":"...",'
    '"confidence":0.9,"evidence":["B25","B39"]}]}'
)

_MAX_TOKENS = 8000       # a sheet has at most a handful of regions
_HEADER_ROWS = 20
_MAX_BODY_ROWS = 300     # bounds the digest so one big sheet can't overflow
_MAX_BLANK_RUNS = 60
_MAX_VALIDATIONS = 30
_FILLABLE = ("data", "sourced")   # fact categories population writes into (see datamodel)
_KINDS = {"kpi_list", "custom_rows", "adjustment_rows", "editable_labels",
          "chart_of_accounts", "other_adjustments", "other"}   # other_adjustments = legacy
_KIND_NORMALIZE = {"other_adjustments": "adjustment_rows"}

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

def _digest(sheet: dict, *, understanding: dict | None = None) -> str:
    """Compact text view for the model: a header band, one line per labelled row
    (with ROW NUMBER, label-cell address, filled + blank value columns, and any
    STRUCTURAL EDITABILITY SIGNALS on the label cell), the runs of
    blank-but-formatted rows, the sheet's data validations — plus, when the L3
    understanding is available, its SECTIONS and ROW ROLES (the knowledge that an
    'EBITDA adjustments' block exists is already extracted; region detection must
    see it). Addresses are authoritative; sizes capped."""
    cells = sheet.get("cells", [])
    signals = _label_signals(sheet)
    # roll-up notes: the SUM range that proves a subtotal aggregates a list — the
    # signal the digest used to strip (formula -> cached number). Member rows
    # already carry [summed_member] via `signals`; here we tag the subtotal row.
    subtotal_note = {b["subtotal_row"]:
                     f"[ROLL-UP sums rows {b['lo']}-{b['hi']} — these are an aggregated LIST]"
                     for b in _subtotal_blocks(sheet)}
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
        if label is not None:
            sig = signals.get((label["row"], label["col"]))
            if sig:
                line += f" [{'/'.join(sig)}]"    # unlocked/validated/placeholder_text/input_fill/summed_member
        if r in subtotal_note:
            line += f" {subtotal_note[r]}"
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

    # TRUE gaps: rows inside the used range with NO stored cells at all. The
    # snapshot only keeps an empty cell when the author styled it, so a KPI
    # block whose free slots carry no styling is invisible to the sections
    # above — these gaps are exactly where such add-slots hide.
    used_max = int(sheet.get("used_max_row") or 0)
    if by_row and used_max:
        lo, hi = min(by_row), min(max(by_row), used_max)
        gaps = [r for r in range(lo, hi + 1) if r not in by_row and r not in blank_only]
        if gaps:
            lines.append("UNSTORED BLANK ROWS (no cells at all inside the used range — possible add slots):")
            for a, b in _runs(gaps)[:_MAX_BLANK_RUNS]:
                lines.append(f"  rows {a}-{b}" if b > a else f"  row {a}")

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

    # The L3 understanding's knowledge of THIS sheet — sections ("EBITDA
    # adjustments", "Custom KPIs") and row roles. Region detection used to
    # re-derive structure blind; now it sees what onboarding already knows.
    if understanding:
        secs = understanding.get("sections") or []
        if secs:
            lines.append("L3 SECTIONS (title | type | range | purpose):")
            for s in secs[:20]:
                lines.append(f"  {str(s.get('title'))[:40]} | {s.get('section_type')} | "
                             f"{s.get('cell_range')} | {str(s.get('purpose'))[:60]}")
        mrows = understanding.get("metric_rows") or []
        if mrows:
            lines.append("L3 ROW ROLES (label_cell | label | value_role | canonical):")
            for m in mrows[:60]:
                lines.append(f"  {m.get('label_cell')} | {str(m.get('label_as_written') or m.get('label'))[:36]} | "
                             f"{m.get('value_role')} | {m.get('canonical_metric') or '-'}")
        rules = understanding.get("author_rules") or []
        if rules:
            lines.append("L3 AUTHOR RULES:")
            for r_ in rules[:8]:
                lines.append(f"  - {str(r_.get('raw_text'))[:100]}")
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


def _detect(digest: str, model: str, tiles: list[tuple[str, bytes]] = ()) -> RegionsOut:
    """One guarded Sonnet call for one sheet — sheet-image tiles first when
    available (the blank invitation block under a "Custom KPIs" heading is a
    VISUAL signal a text digest can miss), digest last — with ONE corrective
    retry when the reply doesn't parse (mirrors population/mapping.py). Raises
    after the retry fails — the caller decides whether that skips the sheet."""
    from app.population.source_understanding import _build_content
    content, n_images = _build_content(digest, list(tiles or []))
    try:
        _, text = guarded_stream(model=model, system=_SYSTEM, content=content,
                                 max_tokens=_MAX_TOKENS,
                                 est_input_chars=len(_SYSTEM) + len(digest),
                                 n_images=n_images, site="region_detection")
    except RuntimeError as e:
        if "truncated at max_tokens" not in str(e):
            raise
        # a dense sheet's region JSON genuinely doesn't fit — one doubled-budget
        # retry beats losing the sheet's regions (mirrors understand_sheet).
        _, text = guarded_stream(model=model, system=_SYSTEM, content=content,
                                 max_tokens=_MAX_TOKENS * 2,
                                 est_input_chars=len(_SYSTEM) + len(digest),
                                 n_images=n_images, site="region_detection")
    try:
        return _parse(text)
    except Exception as e:  # noqa: BLE001 — malformed JSON from the model
        err = str(e)
    logger.warning("regions reply didn't parse (%s) — one corrective retry", err)
    messages = [
        {"role": "user", "content": content},
        {"role": "assistant", "content": text[:4000]},
        {"role": "user", "content": (
            f"That reply was not usable ({err}). Return ONLY the JSON object "
            '{"regions":[...]} for the sheet above — no prose, no fences.'
        )},
    ]
    _, text = guarded_stream(model=model, system=_SYSTEM, messages=messages,
                             max_tokens=_MAX_TOKENS, site="region_detection_retry")
    return _parse(text)


# --- Deterministic conversion / verification ---------------------------------

def _cell_map(sheet: dict) -> dict[tuple[int, int], dict]:
    """(row, col) -> cell for verification lookups."""
    return {(c["row"], c["col"]): c for c in sheet.get("cells", [])}


_STRUCTURAL = {"unlocked", "validated", "input_fill", "summed_member"}   # author-marked editability
# kinds a filler configures BY DESIGN — the LLM's section judgment substitutes for a
# per-cell structural signal when accepting an occupied editable_label row.
_CONFIGURABLE_KINDS = {"kpi_list", "custom_rows"}


def _convert(r: RegionOut, sheet_name: str, cmap: dict[tuple[int, int], dict],
             signals: dict[tuple[int, int], list[str]] | None = None,
             ) -> tuple[dict | None, list[str]]:
    """Turn one model claim into a DB row, verifying it ROW BY ROW against the
    snapshot. Returns (row_or_None, skip_reasons). Layered acceptance — the model
    judges meaning, code corroborates:
      - blank slot        -> the label cell must actually be blank
      - placeholder slot  -> occupied, accepted iff the TEXT reads as a throwaway
                             (deterministic regex) OR a structural signal fires
      - editable_label    -> occupied, accepted ONLY with a structural signal
                             (unlocked/validated/input_fill) — a text judgment
                             alone never makes a real label overwritable
    Offending ROWS drop (with a reason); the region survives while >=1 slot does.
    A total_row inside the range drops that row only."""
    signals = signals or {}
    reasons: list[str] = []
    where = f"{sheet_name}!{r.label_col_cell or '?'}"
    rc = _rc(r.label_col_cell)
    if rc is None:
        return None, [f"{where}: label_col_cell {r.label_col_cell!r} is not an A1 address"]
    label_col = rc[0]

    row_start, row_end = int(r.row_start), int(r.row_end)
    if row_end - row_start + 1 < 1:
        return None, [f"{where}: capacity < 1 (rows {row_start}..{row_end})"]
    total_row = int(r.total_row) if r.total_row is not None else None

    kind = (r.kind or "other").strip().lower()
    kind = _KIND_NORMALIZE.get(kind, kind)
    kind = kind if kind in _KINDS else "other"
    # A CONFIGURABLE METRIC LIST (KPI dashboard / scorecard / custom-metrics) is
    # editable BY DESIGN — its row labels are metrics the filler chooses. Here the
    # LLM's section-level judgment stands in for a per-cell structural signal, so an
    # occupied editable_label row is accepted without unlocked/validated. Fixed
    # statements keep the strict signal requirement (see the editable_label branch).
    configurable = kind in _CONFIGURABLE_KINDS

    # claimed slots; an empty list means "the whole range is blank slots"
    claimed = {int(s.row): s for s in (r.slots or [])}
    if not claimed:
        claimed = {row: SlotOut(row=row, mode="blank") for row in range(row_start, row_end + 1)}

    slots: list[dict] = []
    for row in sorted(claimed):
        s = claimed[row]
        if not (row_start <= row <= row_end):
            reasons.append(f"{where}: slot row {row} outside the region range — dropped")
            continue
        if total_row is not None and row == total_row:
            reasons.append(f"{where}: slot row {row} is the total row — dropped")
            continue
        cell = cmap.get((row, label_col))
        live = effective_value(cell) if cell is not None else None
        live_text = str(live).strip() if isinstance(live, str) else None
        occupied = not _blank(live)
        sigs = set(signals.get((row, label_col), ()))
        mode = (s.mode or "blank").strip().lower()

        if mode == "blank":
            if occupied:
                reasons.append(f"{where}: row {row} claimed blank but the label cell is occupied — dropped")
                continue
            slots.append({"row": row, "mode": "blank", "current_label": None,
                          "evidence": list(s.evidence)[:5]})
        elif mode == "placeholder":
            if not occupied:
                slots.append({"row": row, "mode": "blank", "current_label": None,
                              "evidence": list(s.evidence)[:5]})   # empty placeholder = blank slot
            elif is_placeholder_slot_label(live_text) or (sigs & _STRUCTURAL):
                slots.append({"row": row, "mode": "placeholder", "current_label": live_text,
                              "evidence": sorted(sigs) or list(s.evidence)[:5]})
            else:
                reasons.append(f"{where}: row {row} label {str(live_text)[:30]!r} reads as a real "
                               "line (no placeholder pattern, no structural signal) — dropped")
        elif mode == "editable_label":
            if not occupied:
                slots.append({"row": row, "mode": "blank", "current_label": None,
                              "evidence": list(s.evidence)[:5]})
            elif sigs & _STRUCTURAL:
                slots.append({"row": row, "mode": "editable_label", "current_label": live_text,
                              "evidence": sorted(sigs)})
            elif configurable:
                # configurable metric list — the LLM judged the whole section user-defined
                slots.append({"row": row, "mode": "editable_label", "current_label": live_text,
                              "evidence": sorted(sigs) or [f"{kind}:configurable-section"]})
            else:
                reasons.append(f"{where}: row {row} claimed editable but carries no structural "
                               "signal (unlocked/validated) — dropped")
        else:
            reasons.append(f"{where}: row {row} unknown slot mode {mode!r} — dropped")

    if not slots:
        return None, reasons or [f"{where}: no slot survived verification"]

    value_cols: list[dict] = []
    seen: set[int] = set()
    for addr in r.value_header_cells:
        hc = _rc(addr)
        if hc is None or hc[0] in seen:
            continue
        seen.add(hc[0])
        cell = cmap.get((hc[1], hc[0]))
        hv = effective_value(cell) if cell is not None else None
        d = parse_any_date(hv)
        value_cols.append({"col": hc[0], "parsed_date": d.isoformat() if d else None,
                           "header_label": (str(hv)[:40] if hv not in (None, "") else None)})
    value_cols.sort(key=lambda v: v["col"])
    for i, vc in enumerate(value_cols):
        vc["position"] = i          # left-to-right ordinal — enables positional matching

    return {
        "sheet_name": sheet_name,
        "kind": kind,
        "label_col": label_col,
        "value_cols": value_cols,
        "row_start": row_start,
        "row_end": row_end,
        "total_row": total_row,
        "rules": r.rules,
        "slots": slots,
        "confidence": max(0.0, min(1.0, float(r.confidence))),
        "evidence": list(r.evidence)[:20],
    }, reasons


def detect_sheet_regions(sheet: dict, *, model: str = MODEL_MAP,
                         tiles: list[tuple[str, bytes]] = (),
                         understanding: dict | None = None) -> tuple[list[dict], list[str]]:
    """One sheet end-to-end: enriched digest (+ image tiles + L3 knowledge) →
    guarded call (one retry) → row-level verified regions. Returns
    (rows_without_version_stamp, skip_reasons)."""
    out = _detect(_digest(sheet, understanding=understanding), model, tiles)
    cmap = _cell_map(sheet)
    signals = _label_signals(sheet)
    name = sheet.get("name") or ""
    rows, skipped = [], []
    for r in out.regions:
        row, reasons = _convert(r, name, cmap, signals)
        if row is not None:
            rows.append(row)
        skipped.extend(reasons)
    rows.extend(_adjustment_safety_net(sheet, name, cmap, signals, rows, skipped))
    return rows, skipped


def _covered_rows(rows: list[dict]) -> set[tuple[str, int]]:
    """(sheet, row) pairs already inside a detected region — so the safety net
    never double-claims a block the LLM already handled."""
    cov: set[tuple[str, int]] = set()
    for r in rows:
        for rr in range(int(r["row_start"]), int(r["row_end"]) + 1):
            cov.add((r["sheet_name"], rr))
    return cov


def _header_cell_addr(cmap: dict, col: int, above_row: int) -> str | None:
    """Best-effort period header for a value column: the nearest date-like cell
    scanning up from the block (else the first non-blank text cell)."""
    best = None
    for rr in range(above_row - 1, 0, -1):
        cell = cmap.get((rr, col))
        if cell is None:
            continue
        v = effective_value(cell)
        if parse_any_date(v) is not None:
            return cell.get("address") or f"{column_letter(col)}{rr}"
        if best is None and isinstance(v, str) and v.strip():
            best = cell.get("address") or f"{column_letter(col)}{rr}"
    return best


def _adjustment_safety_net(sheet: dict, name: str, cmap: dict,
                           signals: dict, existing_rows: list[dict],
                           skipped: list[str]) -> list[dict]:
    """Guarantee an adjustment_rows region for every roll-up block that reads as
    an earnings-adjustment list and that the LLM left uncovered — the EBITDA-
    bridge class it misses because the SUM range is invisible in text. The
    summed_member signals make the occupied member labels pass _convert, so this
    routes through the SAME verifier as every other region."""
    covered = _covered_rows(existing_rows)
    added: list[dict] = []
    for blk in _subtotal_blocks(sheet):
        if not _is_adjustment_block(blk, sheet):
            continue
        if any((name, rr) in covered for rr in blk["labelled_rows"]):
            continue                              # the LLM already regioned this block
        headers = [h for c in blk["value_cols"]
                   if (h := _header_cell_addr(cmap, c, blk["lo"]))]
        synth = RegionOut(
            kind="adjustment_rows",
            label_col_cell=f"{column_letter(blk['label_col'])}{blk['lo']}",
            row_start=blk["lo"], row_end=blk["hi"], total_row=blk["subtotal_row"],
            value_header_cells=headers,
            slots=[SlotOut(row=rr, mode="editable_label",
                           evidence=[f"summed by row {blk['subtotal_row']}"])
                   for rr in blk["member_rows"]],
            rules="Earnings-adjustment lines (an EBITDA bridge / normalisation block): "
                  "the filler may add or rename adjustment items; the subtotal sums them.",
            confidence=0.9,
            evidence=[f"{column_letter(blk['label_col'])}{blk['subtotal_row']}"])
        row, reasons = _convert(synth, name, cmap, signals)
        if row is not None:
            row["detection_source"] = "deterministic_subtotal"
            added.append(row)
            covered |= {(name, rr) for rr in range(blk["lo"], blk["hi"] + 1)}
        else:
            skipped.extend(reasons)
    return added


# --- Entry points -------------------------------------------------------------

def detect_and_persist(template_id: str) -> dict:
    """Detect extensible regions for the template's latest version and persist
    them (idempotent replace). Populate-tier spend guard — this is cheap Sonnet
    text, one call per data sheet. Candidate sheets are those with fillable
    (data/sourced) facts in the data model: a sheet population can't write has
    nothing to extend. A single sheet that errors is skipped (not fatal); a
    spend-cap breach still aborts the run."""
    import os
    import tempfile
    from pathlib import Path

    from app.datamodel.derive import _load_snapshot   # lazy: pulls the Aspose parse chain
    from app.datamodel.persist import get_data_model

    # Region detection runs inside /understand — it spends against the ONBOARDING
    # cap, not the (much smaller) populate cap it used to arm by mistake.
    set_guard(SpendGuard(default_onboarding_cap_usd()))
    wb_tmp: Path | None = None
    try:
        dm = get_data_model(template_id, limit=30000)
        if not dm.get("available"):
            # Fresh template: /understand runs this BEFORE any populate has
            # derived a model — derive on demand (deterministic, no LLM) instead
            # of silently losing region kinds + the configurable-list questions.
            from app.datamodel.persist import derive_and_persist
            derive_and_persist(template_id)
            dm = get_data_model(template_id, limit=30000)
        if not dm.get("available"):
            raise RuntimeError("No data model for this template yet — derive it first.")
        version_id = dm["template_version_id"]
        fillable_sheets = {f.get("sheet_name") for f in (dm.get("facts") or [])
                           if f.get("category") in _FILLABLE}

        snapshot = _load_snapshot(version_id, template_id)

        # The template workbook, once, for sheet images: the blank invitation
        # block under a heading is a VISUAL signal the text digest can miss
        # (a KPI sheet whose free slots carry no stored cells). Best-effort —
        # a failed download/render just runs that sheet text-only.
        try:
            _, storage_path, filename = sb.get_latest_file(template_id)
            data = sb.download_workbook(storage_path)
            fd, name_ = tempfile.mkstemp(suffix=Path(filename).suffix or ".xlsx")
            os.close(fd)
            wb_tmp = Path(name_)
            wb_tmp.write_bytes(data)
        except Exception as e:  # noqa: BLE001
            logger.warning("template workbook unavailable for region images (%s) — text-only", e)
            wb_tmp = None

        # The persisted L3 understanding per sheet — sections, row roles, author
        # rules — so detection consumes what onboarding already knows instead of
        # re-deriving structure blind. Best-effort.
        und_by_sheet: dict[str, dict] = {}
        try:
            from app.understanding.persist import get_understanding
            und = get_understanding(template_id)
            for srow in und.get("sheets", []) or []:
                u = srow.get("understanding")
                if u:
                    und_by_sheet[srow.get("sheet_name") or ""] = u
        except Exception as e:  # noqa: BLE001
            logger.info("understanding unavailable for region detection (%s)", e)

        rows: list[dict] = []
        skipped: list[str] = []
        for sheet in snapshot.get("sheets", []):
            name = sheet.get("name")
            if name not in fillable_sheets:
                continue
            tiles: list = []
            if wb_tmp is not None:
                try:
                    from app.understanding.sheet_image import render_sheet_tiles
                    tiles = render_sheet_tiles(wb_tmp, name, max_tiles=3)
                except Exception as e:  # noqa: BLE001 — image is optional
                    logger.warning("region image render failed for %s (%s) — text-only", name, e)
            try:
                srows, sskip = detect_sheet_regions(sheet, tiles=tiles,
                                                    understanding=und_by_sheet.get(name))
            except SpendCapExceeded:
                raise
            except Exception as e:  # noqa: BLE001 — one odd sheet can't sink the run
                logger.exception("region detection failed for sheet %s — skipping", name)
                skipped.append(f"{name}: detection failed ({e})")
                continue
            rows.extend(srows)
            skipped.extend(sskip)

        payload = [{**r, "detection_source": r.get("detection_source", "standalone"),
                    "template_version_id": version_id} for r in rows]
        sb.replace_extensible_regions(version_id, payload)
        logger.info("extensible regions: %d persisted, %d skipped for version %s",
                    len(payload), len(skipped), version_id)

        # ONBOARDING QUESTION per configurable list (owner ruling): the template
        # pre-populates adjustment/KPI lines whose names vary company-by-company —
        # ask ONCE whether these exact lines are demanded or adjustable; the
        # answer persists (content-addressed item_key) and steers both mapping
        # context and the additions path on every future run.
        q_items = []
        try:
            from app.review.items import make_item
            for r in payload:
                if r.get("kind") not in ("kpi_list", "custom_rows", "adjustment_rows"):
                    continue
                labels = [s.get("current_label") for s in (r.get("slots") or [])
                          if isinstance(s, dict) and s.get("current_label")]
                what = ", ".join(labels[:6]) + ("…" if len(labels) > 6 else "")
                q_items.append(make_item(
                    source="onboarding-regions", kind="judgment",
                    question=(f"'{r.get('sheet_name')}' rows {r.get('row_start')}-{r.get('row_end')} "
                              f"is a configurable {r.get('kind')} ({what or 'blank slots'}) — does the "
                              "template demand these exact lines, or may they be renamed/adjusted "
                              "per company?"),
                    why="Pre-populated list names vary by portfolio company; your answer steers "
                        "renames and additions on every future run.",
                    affected={"sheet": r.get("sheet_name"),
                              "rows": [r.get("row_start"), r.get("row_end")]},
                    suggested_answer="adjustable — lines may be renamed per company",
                ))
            if q_items:
                from app.review.items import file_questions
                file_questions(version_id, q_items, family="onboarding-regions")
        except Exception as e:  # noqa: BLE001 — questions are best-effort, never fatal
            logger.warning("could not file configurable-list questions: %s", e)
        return {"template_version_id": version_id, "regions": payload,
                "count": len(payload), "skipped": skipped}
    finally:
        set_guard(None)
        if wb_tmp is not None:
            wb_tmp.unlink(missing_ok=True)


def get_regions(template_id: str) -> dict:
    """Read back the stored regions for the template's latest version."""
    version_id, _, _ = sb.get_latest_file(template_id)
    regions = sb.list_extensible_regions(version_id)
    return {"template_version_id": version_id, "count": len(regions), "regions": regions}
