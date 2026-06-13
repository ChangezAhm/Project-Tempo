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
import re
from collections import Counter

from app import supabase_client as sb
from app.datamodel.identity import fact_key
from app.datamodel.schema import Basis, DataModelResult, DataPoint, DetectedDimensions, Provenance, Scenario
from app.pipeline import get_structure
from app.raw_extraction.column_utils import column_index, column_letter
from app.understanding.persist import get_understanding

logger = logging.getLogger(__name__)

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


def _basis(period: dict | None) -> tuple[Basis, Provenance]:
    ptype = (period.get("period_type") or "").lower() if period else ""
    if ptype == "ytd":
        return Basis.ytd, Provenance.deterministic
    if ptype == "ltm":
        return Basis.trailing, Provenance.deterministic
    return Basis.unknown, Provenance.default   # flow vs point-in-time → LLM/user


def derive_data_model(template_id: str) -> DataModelResult:
    und = get_understanding(template_id)
    if not und.get("available"):
        raise RuntimeError("No Layer-3 understanding yet — run /understand first.")
    structure = get_structure(template_id)
    version_id = und["template_version_id"]

    # Snapshot cell values — to read the real period-header label for every column.
    snap = json.loads(gzip.decompress(sb.download_snapshot(version_id)))
    cell_val: dict[tuple[str, int, int], object] = {}
    for s in snap.get("sheets", []):
        nm = s["name"]
        for c in s.get("cells", []):
            rc = _rc(c.get("address", ""))
            if rc:
                cell_val[(nm, rc[1], rc[0])] = c.get("value")

    period_idx: dict[str, dict[int, dict]] = {}     # L2 parsed_date by (sheet, col)
    for p in structure.get("periods", []):
        period_idx.setdefault(p["sheet_name"], {})[p["col"]] = p
    l2_metric_idx: dict[str, dict[int, dict]] = {}
    for m in structure.get("metric_rows", []):
        l2_metric_idx.setdefault(m["sheet_name"], {})[m["row"]] = m

    facts: list[DataPoint] = []
    flags: list[str] = []
    orphan_cells = 0
    seen: set[tuple[str, str]] = set()

    for srow in und.get("sheets", []):
        sheet = srow["sheet_name"]
        role = srow.get("role")
        u = srow.get("understanding") or {}
        pidx = period_idx.get(sheet, {})
        l2m = l2_metric_idx.get(sheet, {})
        l3_by_row = {rc[1]: m for m in u.get("metric_rows", []) if (rc := _rc(m.get("label_cell") or ""))}

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

        def _label(v: object) -> str | None:
            # Period headers are often dynamic array formulas (a timeline computed
            # from the as-of date), so a formula string is NOT a usable label.
            s = str(v) if v not in (None, "") else ""
            return None if (not s or s.startswith("=")) else s

        def period_for(col: int, row: int) -> dict | None:
            parsed = pidx.get(col, {}).get("parsed_date")
            cp = col_period.get(col)
            if cp:
                lbl = _label(cp["label"]) or _label(cell_val.get((sheet, cp["header_row"], col)))
                return {"label": lbl, "period_type": cp["period_type"], "status": cp["status"], "parsed_date": parsed}
            # infer: a cell exists under the nearest detected header row above → it's
            # a period column even if the header is a dynamic (formula) date.
            above = [h for h in sorted_headers if h < row]
            if not above:
                return None
            if cell_val.get((sheet, max(above), col)) in (None, ""):
                return None
            return {"label": _label(cell_val.get((sheet, max(above), col))),
                    "period_type": default_ptype, "status": None, "parsed_date": parsed}

        for f in u.get("input_fields", []):
            cells = _expand(f.get("cells", []))
            if len(cells) >= _MAX_CELLS_PER_FIELD:
                flags.append(f"{sheet}: field '{f.get('label')}' exceeded {_MAX_CELLS_PER_FIELD} cells; truncated.")
            for col, row in cells:
                cell = f"{column_letter(col)}{row}"
                if (sheet, cell) in seen:
                    continue
                seen.add((sheet, cell))

                l3m = l3_by_row.get(row, {})
                l2mr = l2m.get(row, {})
                metric_label = (l3m.get("label_as_written") or l3m.get("label")
                                or l2mr.get("label_text") or _row_label(cell_val, sheet, row)
                                or f.get("label") or f"row {row}")
                canonical = l3m.get("canonical_metric")
                period = period_for(col, row)
                if period is None:
                    orphan_cells += 1

                scenario = _scenario_from_status(period)
                sc_src = Provenance.deterministic if scenario else Provenance.default
                if scenario is None:
                    scenario = _scenario_from_label(f.get("label"), metric_label)
                    sc_src = Provenance.deterministic if scenario else Provenance.default
                scenario = scenario or Scenario.unknown
                basis, b_src = _basis(period)
                unit = l3m.get("unit") or l2mr.get("unit") or f.get("unit")

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
                    scenario=scenario, basis=basis,
                    entity=None, unit=unit, currency=_currency(unit, l2mr.get("number_format")),
                    value_role=l3m.get("value_role"), sign_convention=l3m.get("sign_convention"),
                    qualification_criteria=l3m.get("qualification_criteria"),
                    definition=l3m.get("definition"), expected_source=l3m.get("expected_source"),
                    needs_value=bool(f.get("needs_value", True)),
                    scenario_source=sc_src, basis_source=b_src,
                    confidence=float(l3m.get("confidence", 0.5) or 0.5),
                ))

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
