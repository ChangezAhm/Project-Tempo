"""Deterministic apply (Build A v2): read the real source value at each linked
address and bind it to the template cell. The LLM produced LOCATIONS only — this
reads the numbers, applies scale/sign, and accounts for every input cell as
filled / skipped / unmatched, with full attribution.
"""

from __future__ import annotations

import re

from app.population.catalogue import effective_value
from app.population.schema import CellLink, FilledCell, PopulationResult

_CELL = re.compile(r"([A-Z]{1,3}\d+)")


def _first_addr(addr: str) -> str:
    """Normalise a cited source cell: strip any 'Sheet!' prefix, take the first
    cell of a range, upper-case. Returns '' if no A1 address is present."""
    s = (addr or "").strip().upper()
    if "!" in s:
        s = s.split("!", 1)[1]
    m = _CELL.search(s)
    return m.group(1) if m else ""


def _num(v):
    """A cell's numeric value, or None if it isn't a number (handles '1,234' and
    parenthesised negatives). bool is never a number."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.strip().replace(",", "")
        neg = t.startswith("(") and t.endswith(")")
        t = t.strip("()").replace("%", "")
        try:
            f = float(t)
            return -f if neg else f
        except ValueError:
            return None
    return None


def apply_links(facts: list[dict], source_snapshot: dict, links: list[CellLink],
                skipped: list[dict]) -> PopulationResult:
    """facts = template input data points; links = LLM cell→cell map; skipped =
    target cells the matcher rejected. Values are read deterministically from the
    source snapshot at the cited addresses."""
    # source values, indexed by (sheet, A1) and with a case-insensitive sheet map
    sval: dict[tuple[str, str], object] = {}
    sheet_by_lower: dict[str, str] = {}
    for s in source_snapshot.get("sheets", []):
        sheet_by_lower[s["name"].lower()] = s["name"]
        for c in s.get("cells", []):
            a = (c.get("address") or "").strip().upper()
            if a:
                sval[(s["name"], a)] = effective_value(c)   # computed result, not formula text

    fact_by: dict[tuple[str, str], dict] = {
        (f["sheet_name"], (f.get("cell") or "").upper()): f for f in facts
    }
    skip_keys = {(s["template_sheet"], (s["template_cell"] or "").upper()) for s in skipped}

    filled: list[FilledCell] = []
    unmatched: list[dict] = []
    linked_keys: set[tuple[str, str]] = set()
    duplicate_links = 0

    def _ref(f: dict) -> dict:
        return {"template_sheet": f["sheet_name"], "template_cell": f.get("cell"),
                "metric": f.get("canonical_metric") or f.get("metric_label"),
                "period_index": f.get("period_index"), "scenario": f.get("scenario")}

    for lk in links:
        tkey = (lk.template_sheet, (lk.template_cell or "").upper())
        f = fact_by.get(tkey)
        if f is None:
            # the model cited a template cell that isn't a known input — don't write
            # into it (could be a header/total); record for the audit.
            unmatched.append({"reason": "linked cell is not a known template input",
                              "template_sheet": lk.template_sheet, "template_cell": lk.template_cell,
                              "source_sheet": lk.source_sheet, "source_cell": lk.source_cell})
            continue
        if tkey in linked_keys:
            duplicate_links += 1   # first (highest-priority) link wins; counted, not silent
            continue
        linked_keys.add(tkey)

        # ATTRIBUTE fill: write the literal label text verbatim — no source-value
        # read, no scale/sign (source_cell is provenance only).
        if lk.literal_text is not None:
            filled.append(FilledCell(
                template_sheet=f["sheet_name"], template_cell=f["cell"],
                value=lk.literal_text, raw_source_value=lk.literal_text,
                source_sheet=lk.source_sheet, source_cell=_first_addr(lk.source_cell) or lk.source_cell,
                metric=f.get("canonical_metric") or f.get("metric_label"),
                period_index=f.get("period_index"), scenario=f.get("scenario"),
                confidence=lk.confidence))
            continue

        ssheet = sheet_by_lower.get(lk.source_sheet.lower(), lk.source_sheet)
        saddr = _first_addr(lk.source_cell)
        raw = sval.get((ssheet, saddr)) if saddr else None
        if raw in (None, ""):
            unmatched.append({"reason": "empty/absent source cell",
                              "source_sheet": ssheet, "source_cell": saddr, **_ref(f)})
            continue
        # Never write a formula/error string into the template. The source cell's
        # cached value can be the formula text or an error (e.g. uncomputable
        # custom functions) — that's not a real value, so report it unmatched.
        if isinstance(raw, str) and raw.strip()[:1] in ("=", "#"):
            unmatched.append({"reason": "source cell holds a formula/error, not a value",
                              "source_sheet": ssheet, "source_cell": saddr, **_ref(f)})
            continue

        # AGGREGATION: sum the primary with each cited component cell. Trust-first —
        # if ANY component is missing/non-numeric we do NOT write a partial total;
        # the cell is reported unmatched so a wrong sum can never be produced.
        raw_out = raw
        if lk.agg_source_cells:
            nums = [_num(raw)]
            missing = None
            if nums[0] is None:
                missing = f"{ssheet}!{saddr}"
            for spec in lk.agg_source_cells:
                if missing is not None:
                    break
                s = (spec or "").strip()
                if "!" in s:
                    csheet_raw, caddr = s.split("!", 1)
                    csheet = sheet_by_lower.get(csheet_raw.strip().lower(), csheet_raw.strip())
                else:
                    csheet, caddr = ssheet, s
                cv = sval.get((csheet, _first_addr(caddr)))
                n = _num(cv)
                if n is None:
                    missing = spec
                else:
                    nums.append(n)
            if missing is not None:
                unmatched.append({"reason": f"aggregation incomplete: component {missing} empty/non-numeric at this period",
                                  "source_sheet": ssheet, "source_cell": saddr, **_ref(f)})
                continue
            # 'avg' = a rate rolled up across months (never summed); default 'sum'
            raw_out = sum(nums) / len(nums) if lk.agg_op == "avg" else sum(nums)

        try:
            value = float(raw_out) * lk.unit_scale * (-1.0 if lk.sign_flip else 1.0)
        except (TypeError, ValueError):
            value = raw_out   # genuine non-numeric text passes through unchanged
        filled.append(FilledCell(
            template_sheet=f["sheet_name"], template_cell=f["cell"], value=value, raw_source_value=raw_out,
            source_sheet=ssheet, source_cell=saddr,
            metric=f.get("canonical_metric") or f.get("metric_label"),
            period_index=f.get("period_index"), scenario=f.get("scenario"), confidence=lk.confidence,
        ))

    # every input fact that was neither filled nor skipped is unmatched
    for f in facts:
        k = (f["sheet_name"], (f.get("cell") or "").upper())
        if k in linked_keys or k in skip_keys:
            continue
        unmatched.append({"reason": "no source match", **_ref(f)})

    summary = {"facts": len(facts), "filled": len(filled),
               "unmatched": len(unmatched), "skipped": len(skipped)}
    if duplicate_links:
        summary["duplicate_links_dropped"] = duplicate_links
    return PopulationResult(filled=filled, unmatched=unmatched, skipped=skipped, summary=summary)
