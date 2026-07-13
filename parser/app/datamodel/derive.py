"""Deterministic derivation of the dimensional data model.

Source of truth for *which cells are inputs* = the Layer-3 understanding (the
LLM's input_fields are comprehensive where the rigid L2 detector isn't). The
*period coordinate* of a column comes from the detected periods where present,
else by reading the period header row out of the snapshot (so every monthly
column gets its label, not just the few the detector pinned). Scenario falls
back to the field/metric label ("Budget —", "Actual —") when the period status
doesn't carry it. Metric interpretation comes from the matching L3 MetricRow.

No LLM here — genuinely ambiguous dimensions are left `unknown` for the
LLM-enrichment pass / the contract to refine.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from app import supabase_client as sb
from app.datamodel.identity import fact_key
from app.datamodel.schema import Basis, DataModelResult, DataPoint, DetectedDimensions, Provenance, Scenario
from app.pipeline import get_structure
from app.raw_extraction.column_utils import column_index, column_letter
from app.raw_extraction.workbook_parser import parse_workbook
from app.snapshot import workbook_to_snapshot
from app.understanding.persist import get_understanding

logger = logging.getLogger(__name__)

# Bump whenever the derivation logic changes — population auto-re-derives a data
# model whose stored version is older than this, so code changes take effect on the
# next run instead of silently using a stale map.
DERIVATION_VERSION = 7

_CELL = re.compile(r"^([A-Z]+)(\d+)$")
_MAX_CELLS_PER_FIELD = 4000
_CCY = [("£", "GBP"), ("GBP", "GBP"), ("$", "USD"), ("USD", "USD"), ("€", "EUR"), ("EUR", "EUR")]


def _rc(addr: str) -> tuple[int, int] | None:
    """'AD20' -> (col=30, row=20), 1-based col."""
    m = _CELL.match((addr or "").strip().upper())
    return (column_index(m.group(1)), int(m.group(2))) if m else None


def _expand(cells: list[str]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for tok in cells or []:
        parts = str(tok).strip().upper().split(":")
        a = _rc(parts[0])
        if not a:
            continue
        if len(parts) == 1:
            out.append(a)
            continue
        b = _rc(parts[1]) or a
        (c1, r1), (c2, r2) = a, b
        for r in range(min(r1, r2), max(r1, r2) + 1):
            for c in range(min(c1, c2), max(c1, c2) + 1):
                out.append((c, r))
                if len(out) >= _MAX_CELLS_PER_FIELD:
                    return out
    return out


def _row_label(cell_val: dict, sheet: str, row: int) -> str | None:
    """Leftmost short text cell in a row = its line-item label. Used when neither
    L2 nor L3 enumerated a metric for the row, so different lines stay distinct."""
    for c in range(1, 13):
        v = cell_val.get((sheet, row, c))
        if isinstance(v, str) and v.strip() and not v.startswith("="):
            return v.strip()[:80]
    return None


def _currency(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        for sym, code in _CCY:
            if sym in t or sym in t.upper():
                return code
    return None


def _parse_header_date(val) -> str | None:
    """'YYYY-MM' from a period header's COMPUTED value — a date/datetime, an ISO
    string ('2025-01-31T00:00:00', what a formula date header caches), or an Excel
    serial. This is how a relative-timeline month header (a formula) yields a real
    date. None if it isn't a date."""
    if val is None or isinstance(val, bool):
        return None
    if hasattr(val, "year") and hasattr(val, "month"):     # date / datetime
        return f"{val.year:04d}-{val.month:02d}"
    if isinstance(val, str):
        m = re.match(r"\s*(\d{4})-(\d{1,2})", val)
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}" if m else None
    if isinstance(val, (int, float)) and 29000 <= val <= 60000:
        from datetime import datetime, timedelta
        d = datetime(1899, 12, 30) + timedelta(days=int(val))
        return f"{d.year:04d}-{d.month:02d}"
    return None


def _iso_label(iso: str | None) -> str | None:
    """A clean 'Jan-25' display label from a 'YYYY-MM' key."""
    if not iso:
        return None
    from datetime import datetime
    try:
        return datetime.strptime(iso[:7], "%Y-%m").strftime("%b-%y")
    except ValueError:
        return iso


def _grain_from_dates(isos) -> str | None:
    """Grain inferred from the SPACING of real period dates — deterministic and
    reliable, so it overrides the LLM's granularity guess (which called the P&L's
    monthly columns 'annual')."""
    ds = sorted({i[:7] for i in isos if i})
    if len(ds) < 2:
        return None
    def ym(s: str) -> int:
        y, m = s.split("-")[:2]
        return int(y) * 12 + int(m)
    gaps = [b - a for a, b in ((ym(x), ym(y)) for x, y in zip(ds, ds[1:])) if b - a > 0]
    if not gaps:
        return None
    g = Counter(gaps).most_common(1)[0][0]
    return "monthly" if g <= 1 else "quarterly" if g <= 3 else "annual"


_SCEN_ENUM = {"budget": Scenario.budget, "forecast": Scenario.forecast}

# Sheet roles that are NOT a fill surface: blank/literal cells there are scratch or
# derived state, not input slots. `input`/`mixed` sheets pass; membership in the
# workbook-level input_surface_sheets overrides any per-sheet role.
_NON_INPUT_ROLES = {"calc", "lookup", "data_dump", "cover", "instructions"}


def _role_blocks_writes(role: str | None, sheet: str, input_surface: set[str]) -> bool:
    """True when populate must not write blank/literal cells on this sheet. The
    workbook-level judgment wins: a sheet the synthesis names as input surface is
    fillable whatever its role. A missing role fails OPEN (legacy understandings)."""
    if sheet in input_surface:
        return False
    return (role or "").lower() in _NON_INPUT_ROLES


# CONTROL cells parametrise a report's VIEW (scenario/company/mode selectors,
# override flags) — they are neither collected inputs nor derived outputs, so
# they must never generate populate demand. PLACEHOLDER cells are the empty,
# generically-named slots of an extensible area ("KPI Label 3", "Custom Metric
# Amount 1") — the additions/region path fills those, not the data model. Both
# → category 'config' (not fillable). High precision by design; every hit is
# review-flagged and a correction (patch category='data') re-opens it.
_CONTROL_RES = [
    re.compile(r"(?i)\bmode\b"),                         # POC Mode, View Mode
    re.compile(r"(?i)\bselection\b"),                    # Scenario Selection N
    re.compile(r"(?i)^\s*selected\b"),                   # Selected <X> Company:
    re.compile(r"(?i)\boverride\b|\bflags?\b"),          # …override flags
    re.compile(r"(?i)\btoggle\b|\bsettings?\b|\bconfig\b|\bselector\b"),
]
_PLACEHOLDER_RES = [
    re.compile(r"(?i)^(?:custom\s+)?(?:kpi|metric|line\s*item|item)(?:\s+(?:label|amount|name))?\s*#?\d+(?:\s*\[[^\]]*\])?\s*$"),
    re.compile(r"(?i)^(?:kpi|metric|line|item|label)\s+label\s*#?\d+\s*$"),   # 'KPI Label 1'
    re.compile(r"(?i)\blabel\s*#?\d+\s*$"),                                   # '… - Label 2'
    re.compile(r"(?i)^\[[^\]]*\]\s*$"),                                       # [Specify]
    re.compile(r"(?i)^(?:specify|tbd|placeholder|n/?a)\s*:?\s*$"),
    re.compile(r"…\s*$"),                                                # trailing ellipsis
]


def _is_control_label(label: str | None) -> bool:
    t = (label or "").strip()
    return bool(t) and any(rx.search(t) for rx in _CONTROL_RES)


def _is_placeholder_label(label: str | None) -> bool:
    t = (label or "").strip()
    return bool(t) and any(rx.search(t) for rx in _PLACEHOLDER_RES)


def apply_row_scenario_layout(facts: list, role_by_sheet: dict,
                              l3_tags: dict[str, dict[int, tuple[str | None, int | None]]] | None = None) -> int:
    """Handle sheets that encode scenario BY ROW — a metric row paired with a
    scenario restatement row (a bare 'Budget' row, 'Budget (Revenue)', or a budget
    block repeating the lines). WHO decides is layered:
      1. the model's PER-ROW judgment (``l3_tags``: sheet -> row -> (scenario,
         parent_row)) — it reads ANY layout;
      2. the row's own explicit label tag — deterministic validation + fallback;
      3. the column's explicit tag (a budget-typed period header);
      4. nothing — unknown. A scenario REGION painted over such a sheet is ignored:
         a rectangle cannot express a row-interleaved layout and it once swallowed
         the actual COGS row.
    Guardrail — a model claim never silently MERGES identities: a variant inherits
    its parent's metric identity only when the label confirms the tag or the two
    labels already match (a budget block repeating names); otherwise the row keeps
    its own identity and only the scenario is applied. Sheets with no tagged rows
    are untouched (regions still apply there — the column-block layout they were
    designed for). Mutates facts in place; fact_keys are recomputed."""
    from app.datamodel.identity import fact_key
    from app.population.catalogue import _norm_label, parse_scenario_variant
    from app.raw_extraction.column_utils import column_letter

    l3_tags = l3_tags or {}
    by_sheet: dict[str, list] = defaultdict(list)
    for f in facts:
        by_sheet[f.sheet_name].append(f)
    changed = 0
    for sheet, fs in by_sheet.items():
        tags = l3_tags.get(sheet, {})
        rows: dict[int, list] = defaultdict(list)
        for f in fs:
            rows[f.row].append(f)

        # per-row claim: (scenario, parent_row, parent_name, label_confirms)
        variants: dict[int, tuple[Scenario, int | None, str | None, bool]] = {}
        row_scen: dict[int, Scenario] = {}       # AI row-level scenario, no parent claim
        for r, lst in rows.items():
            lab_scen, lab_pname = parse_scenario_variant(lst[0].metric_label)
            ai_scen, ai_prow = tags.get(r, (None, None))
            scen = ai_scen if ai_scen in ("budget", "forecast") else lab_scen
            if scen in _SCEN_ENUM and (ai_prow is not None or lab_scen is not None or lab_pname):
                variants[r] = (_SCEN_ENUM[scen], ai_prow, lab_pname, lab_scen is not None)
            elif ai_scen in ("actual", "budget", "forecast"):
                row_scen[r] = {"actual": Scenario.actual, "budget": Scenario.budget,
                               "forecast": Scenario.forecast}[ai_scen]
        if not variants:
            continue
        metric_rows = sorted(r for r in rows if r not in variants)
        by_name = {}
        for r in metric_rows:
            by_name.setdefault(_norm_label(rows[r][0].metric_label), rows[r][0])
        touched = []
        for r in sorted(rows):
            lst = rows[r]
            if r in variants:
                scen, prow, pname, label_confirms = variants[r]
                # parent: the model's cited row first, then the named label, then above
                parent = rows[prow][0] if (prow in rows and prow != r) else None
                if parent is None and pname:
                    parent = by_name.get(_norm_label(pname))
                if parent is None:
                    above = [mr for mr in metric_rows if mr < r]
                    parent = rows[above[-1]][0] if above else None
                inherit = parent is not None and (
                    label_confirms or pname is not None
                    or _norm_label(lst[0].metric_label) == _norm_label(parent.metric_label))
                for f in lst:
                    f.scenario = scen
                    f.scenario_source = (Provenance.deterministic if label_confirms
                                         else Provenance.llm)
                    if inherit:
                        f.metric_label = parent.metric_label
                        f.canonical_metric = parent.canonical_metric
                    touched.append(f)
            else:
                own = _scenario_from_label(lst[0].metric_label)
                for f in lst:
                    # row label > model row tag > column tag > unknown
                    col_tag = Scenario.budget if (f.period_type or "").lower() == "budget" else None
                    new = own or row_scen.get(r) or col_tag or Scenario.unknown
                    if f.scenario != new:
                        f.scenario = new
                        f.scenario_source = (Provenance.deterministic if (own or col_tag)
                                             else Provenance.llm if r in row_scen
                                             else Provenance.default)
                        touched.append(f)
        for f in touched:
            f.fact_key = fact_key(
                sheet_role=role_by_sheet.get(sheet), metric=f.canonical_metric or f.metric_label,
                period=(f.parsed_date or f.period_label
                        or (f"c{column_letter(f.col)}" if f.period_type else None)),
                scenario=f.scenario.value, basis=f.basis.value, entity=None,
            )
        changed += len(touched)
    return changed


def _scenario_from_status(period: dict | None) -> Scenario | None:
    if not period:
        return None
    status = (period.get("status") or "").lower()
    ptype = (period.get("period_type") or "").lower()
    if status == "budget" or ptype == "budget":
        return Scenario.budget
    if status == "future":
        return Scenario.forecast
    if status in ("historical", "current", "ytd", "ltm"):
        return Scenario.actual
    return None


def _scenario_from_label(*texts: str | None) -> Scenario | None:
    for t in texts:
        tl = (t or "").lower()
        if "budget" in tl:
            return Scenario.budget
        if "forecast" in tl or "outlook" in tl:
            return Scenario.forecast
        if "actual" in tl:
            return Scenario.actual
    return None


def _region_scenario(scenario: str | None) -> Scenario | None:
    """Map a scenario_region's declared scenario, strictly. Ambiguous labels
    ('selectable', 'Actual/Budget', 'segment-driven') stay unknown — honest."""
    s = (scenario or "").strip().lower()
    if not s or "select" in s or "driven" in s or "/" in s or " or " in s:
        return None
    if s.startswith("actual"):
        return Scenario.actual
    if s.startswith("budget"):
        return Scenario.budget
    if s.startswith("forecast") or s.startswith("plan") or s.startswith("outlook"):
        return Scenario.forecast
    return None


def _basis(period: dict | None) -> tuple[Basis, Provenance]:
    ptype = (period.get("period_type") or "").lower() if period else ""
    if ptype == "ytd":
        return Basis.ytd, Provenance.deterministic
    if ptype == "ltm":
        return Basis.trailing, Provenance.deterministic
    return Basis.unknown, Provenance.default   # flow vs point-in-time → LLM/user


# Data-connector functions: a cell whose formula calls one is fed from the
# connected system (Chronograph / Power-BI), not typed by hand.
_CONNECTOR = re.compile(r"(?i)CX_GET|CVC\.GET|GETPIVOTDATA|CUBEVALUE|CUBEMEMBER")


def _bounds(rng: str | None) -> tuple[int, int, int, int] | None:
    """'W17:BN41' -> (r1, c1, r2, c2). Handles a single-cell range too."""
    if not rng:
        return None
    parts = str(rng).split(":")
    a = _rc(parts[0])
    b = _rc(parts[1]) if len(parts) > 1 else a
    if not a or not b:
        return None
    return (min(a[1], b[1]), min(a[0], b[0]), max(a[1], b[1]), max(a[0], b[0]))


def _section_scenario(title: str | None) -> Scenario | None:
    t = (title or "").lower()
    if "budget" in t:
        return Scenario.budget
    if "forecast" in t or "outlook" in t:
        return Scenario.forecast
    if "actual" in t:
        return Scenario.actual
    return None


def _section_category(section_type: str | None, title: str | None) -> str | None:
    """Region-level category hint from the LLM's own section labels."""
    st = (section_type or "").lower()
    t = (title or "").lower()
    if st in ("instructions", "cover"):
        return "exclude"
    if st == "reconciliation" or "automatic" in t or "calculation" in t or "calc" in t:
        return "computed"
    return None


def _basis_from_section_type(section_type: str | None) -> Basis | None:
    """A line item's basis follows the statement it lives in: balance-sheet-style
    statements are point-in-time stocks; P&L / cash-flow are period flows."""
    st = (section_type or "").lower()
    if st in ("balance_sheet", "cap_table", "debt_schedule"):
        return Basis.point_in_time
    if st in ("income_statement", "cash_flow"):
        return Basis.flow
    return None


def _load_snapshot(version_id: str, template_id: str) -> dict:
    """The parsed snapshot, re-parsing the workbook if none is stored — so a
    missing/deleted snapshot never blocks derivation (the workbook is truth)."""
    try:
        return json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    except Exception as e:  # noqa: BLE001
        logger.warning("no stored snapshot for %s (%s) — parsing workbook in-memory", version_id, e)
        _, path, fn = sb.get_latest_file(template_id)
        fd, tmp = tempfile.mkstemp(suffix=Path(fn).suffix or ".xlsx")
        os.close(fd)
        p = Path(tmp)
        p.write_bytes(sb.download_workbook(path))
        try:
            return workbook_to_snapshot(parse_workbook(p))
        finally:
            p.unlink(missing_ok=True)


def derive_data_model(template_id: str) -> DataModelResult:
    und = get_understanding(template_id)
    if not und.get("available"):
        raise RuntimeError("No Layer-3 understanding yet — run /understand first.")
    structure = get_structure(template_id)
    version_id = und["template_version_id"]
    # The workbook-level judgment of WHERE the portfolio company actually enters
    # data — used by the sheet-role write gate (a sheet named here is fillable
    # whatever its per-sheet role says).
    input_surface = set((und.get("workbook") or {}).get("input_surface_sheets") or [])

    # Snapshot cell values — to read the real period-header label for every column.
    snap = _load_snapshot(version_id, template_id)
    cell_val: dict[tuple[str, int, int], object] = {}
    cell_cached: dict[tuple[str, int, int], object] = {}
    cell_formula: dict[tuple[str, int, int], str] = {}
    for s in snap.get("sheets", []):
        nm = s["name"]
        for c in s.get("cells", []):
            rc = _rc(c.get("address", ""))
            if rc:
                cell_val[(nm, rc[1], rc[0])] = c.get("value")
                cv = c.get("cached_value")
                if cv is not None:
                    cell_cached[(nm, rc[1], rc[0])] = cv
                if c.get("formula"):
                    cell_formula[(nm, rc[1], rc[0])] = c["formula"]

    period_idx: dict[str, dict[int, dict]] = {}     # L2 parsed_date by (sheet, col)
    for p in structure.get("periods", []):
        period_idx.setdefault(p["sheet_name"], {})[p["col"]] = p
    l2_metric_idx: dict[str, dict[int, dict]] = {}
    for m in structure.get("metric_rows", []):
        l2_metric_idx.setdefault(m["sheet_name"], {})[m["row"]] = m

    # Deterministic input fields (the six-signal metadata detector) — UNIONed with
    # the LLM's input_fields below. The LLM under-enumerates on big/repetitive
    # sheets; the metadata signals (unlocked, fill colour, formula-graph, validation)
    # are exhaustive, so combining them stops "missing data points".
    det_by_sheet: dict[str, list[dict]] = defaultdict(list)
    for df in structure.get("fields", []):
        det_by_sheet[df.get("sheet_name")].append(df)

    facts: list[DataPoint] = []
    flags: list[str] = []
    orphan_cells = 0
    seen: set[tuple[str, str]] = set()
    role_by_sheet: dict[str, str | None] = {}
    l3_row_tags: dict[str, dict[int, tuple[str | None, int | None]]] = {}
    gated_by_role: dict[str, int] = {}
    config_by_kind: dict[str, int] = {}    # {'control': n, 'placeholder': n, 'selector': n}

    for srow in und.get("sheets", []):
        sheet = srow["sheet_name"]
        role = srow.get("role")
        role_by_sheet[sheet] = role
        u = srow.get("understanding") or {}
        pidx = period_idx.get(sheet, {})
        l2m = l2_metric_idx.get(sheet, {})
        l3_by_row = {rc[1]: m for m in u.get("metric_rows", []) if (rc := _rc(m.get("label_cell") or ""))}
        # the model's PER-ROW scenario judgment (any layout) — primary signal for
        # the row-scenario post-process; label parsing validates / fills gaps.
        for _r, _m in l3_by_row.items():
            _scen = (_m.get("scenario") or "").strip().lower() or None
            _vrc = _rc(_m.get("variant_of_cell") or "")
            if _scen or _vrc:
                l3_row_tags.setdefault(sheet, {})[_r] = (_scen, _vrc[1] if _vrc else None)

        # Detected column periods + the header rows they sit on.
        col_period: dict[int, dict] = {}
        header_rows: set[int] = set()
        grains: Counter = Counter()
        for p in u.get("periods", []):
            rc = _rc(p.get("cell") or "")
            if not rc or (p.get("orientation") or "column") == "row":
                continue
            col_period[rc[0]] = {"label": p.get("label"), "period_type": p.get("granularity"),
                                 "status": p.get("status"), "header_row": rc[1]}
            header_rows.add(rc[1])
            if p.get("granularity"):
                grains[p["granularity"]] += 1
        default_ptype = grains.most_common(1)[0][0] if grains else None
        sorted_headers = sorted(header_rows)

        # Real period DATES read from the header cells' COMPUTED values (a relative
        # timeline's month headers are formulas; their date is in cached_value, not the
        # formula text). This gives parsed_date + a grain inferred from the spacing that
        # OVERRIDES the LLM's granularity guess (which mislabelled monthly P&L 'annual').
        col_date: dict[int, str] = {}
        for (_nm, _r, _c), _cv in cell_cached.items():
            if _nm == sheet and _r in header_rows:
                iso = _parse_header_date(_cv)
                if iso:
                    col_date[_c] = iso
        sheet_date_grain = _grain_from_dates(col_date.values())

        # Sections: the LLM's blocks — used for category + statement-type basis;
        # smallest (most specific) wins on overlap.
        secs = []
        for sec in u.get("sections", []):
            b = _bounds(sec.get("cell_range"))
            if b:
                area = (b[2] - b[0] + 1) * (b[3] - b[1] + 1)
                secs.append((area, b, _section_category(sec.get("section_type"), sec.get("title")),
                             sec.get("section_type") or ""))
        secs.sort(key=lambda x: x[0])

        def section_for(col: int, row: int):
            for _area, (r1, c1, r2, c2), cat, st in secs:
                if r1 <= row <= r2 and c1 <= col <= c2:
                    return cat, st
            return None, None

        # Scenario regions: the LLM delineated Actual/Budget/Forecast areas from
        # the image — the primary, general scenario signal (any layout).
        scen_regions = []
        for sr in u.get("scenario_regions", []):
            b = _bounds(sr.get("cell_range"))
            sc = _region_scenario(sr.get("scenario"))
            if b and sc:
                area = (b[2] - b[0] + 1) * (b[3] - b[1] + 1)
                scen_regions.append((area, b, sc))
        scen_regions.sort(key=lambda x: x[0])

        def scenario_region_for(col: int, row: int):
            for _area, (r1, c1, r2, c2), sc in scen_regions:
                if r1 <= row <= r2 and c1 <= col <= c2:
                    return sc
            return None

        def _label(v: object) -> str | None:
            # Period headers are often dynamic array formulas (a timeline computed
            # from the as-of date), so a formula string is NOT a usable label.
            s = str(v) if v not in (None, "") else ""
            return None if (not s or s.startswith("=")) else s

        def period_for(col: int, row: int) -> dict | None:
            cp = col_period.get(col)
            iso = col_date.get(col)
            parsed = iso or pidx.get(col, {}).get("parsed_date")
            # deterministic date-spacing grain wins over the LLM's guess.
            grain = sheet_date_grain or (cp.get("period_type") if cp else None) or default_ptype
            if cp:
                # a real date makes the cleanest label; fall back to the LLM's/header text.
                lbl = (_iso_label(iso) or _label(cp["label"])
                       or _label(cell_val.get((sheet, cp["header_row"], col))))
                return {"label": lbl, "period_type": grain, "status": cp["status"], "parsed_date": parsed}
            # infer: a cell exists under the nearest detected header row above → it's
            # a period column even if the header is a dynamic (formula) date.
            above = [h for h in sorted_headers if h < row]
            if not above:
                return None
            hr = max(above)
            if cell_val.get((sheet, hr, col)) in (None, "") and iso is None:
                return None
            lbl = _iso_label(iso) or _label(cell_val.get((sheet, hr, col)))
            return {"label": lbl, "period_type": grain, "status": None, "parsed_date": parsed}

        def _emit(col: int, row: int, llm_field: dict | None) -> None:
            """Create one DataPoint for an input cell. ``llm_field`` carries the
            LLM's semantics when the cell came from understanding; None for a
            cell found purely by the deterministic metadata detector."""
            nonlocal orphan_cells
            cell = f"{column_letter(col)}{row}"
            if (sheet, cell) in seen:
                return
            seen.add((sheet, cell))

            l3m = l3_by_row.get(row, {})
            l2mr = l2m.get(row, {})
            metric_label = (l3m.get("label_as_written") or l3m.get("label")
                            or l2mr.get("label_text") or _row_label(cell_val, sheet, row)
                            or (llm_field or {}).get("label") or f"row {row}")
            canonical = l3m.get("canonical_metric")
            period = period_for(col, row)
            if period is None:
                orphan_cells += 1

            sec_cat, sec_type = section_for(col, row)
            # scenario precedence: LLM scenario region (primary, read from the
            # image) > explicit row/field label > period status.
            scenario = (scenario_region_for(col, row)
                        or _scenario_from_label((llm_field or {}).get("label"), metric_label)
                        or _scenario_from_status(period))
            sc_src = Provenance.deterministic if scenario else Provenance.default
            scenario = scenario or Scenario.unknown
            # basis: period_type (ytd/ltm) first, else the statement type the line sits in.
            basis, b_src = _basis(period)
            if basis == Basis.unknown:
                bt = _basis_from_section_type(sec_type)
                if bt:
                    basis, b_src = bt, Provenance.deterministic
            unit = l3m.get("unit") or l2mr.get("unit") or (llm_field or {}).get("unit")
            # Category is decided per CELL by what the cell actually IS, so population
            # only ever writes into genuine data-entry inputs:
            #   - connector-fed (CX_GET …)      → sourced  (system-fed, overridable → fillable)
            #   - instructions/cover region     → exclude  (never filled)
            #   - holds a real (non-connector) formula → computed (a calculated OUTPUT — NEVER
            #                                     overwrite it; stops clobbering formula sheets)
            #   - blank / typed literal on a NON-INPUT sheet (calc/lookup/data_dump/
            #     cover/instructions role, not on the input surface) → staging —
            #     a live cell but never a write target; a template correction
            #     (patch category='data') re-opens it per-fact
            #   - blank / typed literal         → data     (a data-entry slot → fillable)
            #   - a CONTROL/selector (scenario/company/mode picker, override flag)
            #     or a generic PLACEHOLDER slot ("KPI Label 3") → config — it
            #     parametrises or extends the view, it is not a data input, so it
            #     must never demand a source value. Correction (patch
            #     category='data') re-opens it.
            formula = cell_formula.get((sheet, row, col), "")
            _cfg_kind = (
                "control" if _is_control_label(metric_label)
                else "placeholder" if _is_placeholder_label(metric_label)
                else None)
            if _CONNECTOR.search(formula):
                category = "sourced"
            elif sec_cat == "exclude":
                category = "exclude"
            elif formula:
                category = "computed"
            elif _cfg_kind:
                category = "config"
                config_by_kind[_cfg_kind] = config_by_kind.get(_cfg_kind, 0) + 1
            elif _role_blocks_writes(role, sheet, input_surface):
                category = "staging"
                gated_by_role[(role or "?").lower()] = gated_by_role.get((role or "?").lower(), 0) + 1
            else:
                category = "data"

            needs = (bool(llm_field.get("needs_value", True)) if llm_field
                     else cell_val.get((sheet, row, col)) in (None, "", 0))

            facts.append(DataPoint(
                fact_key=fact_key(
                    sheet_role=role, metric=canonical or metric_label,
                    # period identity falls back to the column when the label is
                    # dynamic (timeline-driven), so each month stays a distinct fact.
                    period=((period or {}).get("parsed_date") or (period or {}).get("label")
                            or (f"c{column_letter(col)}" if period else None)),
                    scenario=scenario.value, basis=basis.value, entity=None,
                ),
                sheet_name=sheet, cell=cell, row=row, col=col, metric_row_id=l2mr.get("id"),
                metric_label=metric_label, canonical_metric=canonical,
                period_index=None,  # assigned post-loop (relative ordinal on the timeline)
                period_label=(period or {}).get("label"), parsed_date=(period or {}).get("parsed_date"),
                period_type=(period or {}).get("period_type"),
                scenario=scenario, basis=basis, category=category,
                entity=None, unit=unit, currency=_currency(unit, l2mr.get("number_format")),
                value_role=l3m.get("value_role"), sign_convention=l3m.get("sign_convention"),
                qualification_criteria=l3m.get("qualification_criteria"),
                definition=l3m.get("definition"), expected_source=l3m.get("expected_source"),
                needs_value=needs,
                scenario_source=sc_src, basis_source=b_src,
                confidence=float(l3m.get("confidence", 0.5) or 0.5),
            ))

        # 1) LLM-detected inputs (semantic, image-grounded).
        for f in u.get("input_fields", []):
            cells = _expand(f.get("cells", []))
            if len(cells) >= _MAX_CELLS_PER_FIELD:
                flags.append(f"{sheet}: field '{f.get('label')}' exceeded {_MAX_CELLS_PER_FIELD} cells; truncated.")
            for col, row in cells:
                _emit(col, row, f)

        # 2) Deterministic metadata-detected inputs (UNION) — captures the cells the
        # LLM under-enumerated; already-seen cells are skipped (LLM semantics win).
        for df in det_by_sheet.get(sheet, []):
            drow = df.get("row")
            if drow is None:
                continue
            for col in (df.get("input_columns") or []):
                _emit(int(col), int(drow), None)

    if orphan_cells:
        flags.append(f"{orphan_cells} input cells got no period (no header row above them) — review period detection.")

    # Period is RELATIVE: assign each period-bearing column an ordinal on its
    # sheet's timeline (left→right). The absolute month resolves only at
    # population time, from the user's as-of date.
    cols_by_sheet: dict[str, set[int]] = {}
    for f in facts:
        if f.period_type:
            cols_by_sheet.setdefault(f.sheet_name, set()).add(f.col)
    index_map = {s: {c: i for i, c in enumerate(sorted(cs))} for s, cs in cols_by_sheet.items()}
    for f in facts:
        if f.period_type:
            f.period_index = index_map[f.sheet_name].get(f.col)

    # Sheet-role write gate accounting — exclusions must never be silent.
    if gated_by_role:
        detail = ", ".join(f"{r}: {n}" for r, n in sorted(gated_by_role.items()))
        flags.append(
            f"{sum(gated_by_role.values())} input-looking cells on non-input sheets were "
            f"gated from population ({detail}) — if any are real inputs, re-categorise via "
            "a template correction (patch category='data')."
        )
    if config_by_kind:
        detail = ", ".join(f"{k}: {n}" for k, n in sorted(config_by_kind.items()))
        flags.append(
            f"{sum(config_by_kind.values())} control/placeholder cells were classified as "
            f"config ({detail}) and excluded from population — selectors, mode/scenario "
            "toggles, override flags, and empty custom-metric slots are not data inputs. "
            "If any is a real input, re-categorise via a correction (patch category='data')."
        )

    roleless = sorted(s for s, r in role_by_sheet.items() if not r)
    if roleless:
        flags.append(
            f"{len(roleless)} sheet(s) have no role in the understanding "
            f"({', '.join(roleless[:5])}{'…' if len(roleless) > 5 else ''}) — the write "
            "gate fails open there; re-run Understand to enable it."
        )

    # Row-scenario layout (metric row + bare 'Budget' row): variant rows inherit
    # their parent's metric identity; row labels beat painted scenario regions.
    adjusted = apply_row_scenario_layout(facts, role_by_sheet, l3_row_tags)
    if adjusted:
        flags.append(f"{adjusted} facts re-dimensioned for the row-scenario layout "
                     "(bare Budget/Forecast rows inherit the metric row above).")

    timeline_relative = any(f.period_type and not f.parsed_date for f in facts)
    if timeline_relative:
        flags.append("Periods are timeline-driven (computed from the as-of date), so they are stored "
                     "RELATIVE (period_index). Absolute months resolve at population, from the as-of date.")

    dims = DetectedDimensions(
        archetype=(und.get("workbook") or {}).get("archetype"),
        timeline_relative=timeline_relative,
        base_currency=(Counter(f.currency for f in facts if f.currency).most_common(1) or [(None,)])[0][0],
        entities=[],
        scenarios=sorted({f.scenario.value for f in facts}),
        period_grains=sorted({f.period_type for f in facts if f.period_type}),
        sheet_count=len(und.get("sheets", [])), fact_count=len(facts), review_flags=flags,
    )
    return DataModelResult(dimensions=dims, facts=facts)
