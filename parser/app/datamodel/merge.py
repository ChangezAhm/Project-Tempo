"""Apply template-level corrections to a freshly-derived data model.

A correction is a content-based match + patch. It re-applies on every derive, so
a fix made once persists across re-uploads. Matching is on derived content
(metric / sheet / scenario / fact_key), never the cell, so corrections survive
cells moving between template versions. Corrections that match nothing are
returned as `unmatched` — surfaced for review, never silently dropped.

Operates on fact dicts (post model_dump) so enum patches stay simple.
"""

from __future__ import annotations

# When these dimensions are patched, mark their provenance as user-set.
_SOURCE_FIELD = {"scenario": "scenario_source", "basis": "basis_source"}


def _matches(fact: dict, match: dict) -> bool:
    # A fact matches when every specified field equals the fact's value. An empty
    # match {} matches every fact (a workbook-wide correction, e.g. base currency).
    return all(fact.get(k) == v for k, v in match.items())


def apply_corrections(facts: list[dict], corrections: list[dict]) -> tuple[list[dict], set[str], list[dict]]:
    """Returns (patched_facts, applied_ids, unmatched_corrections)."""
    applied: set[str] = set()
    for c in corrections:
        cid = c["id"]
        match = c.get("match") or {}
        patch = c.get("patch") or {}
        hit = False
        for f in facts:
            if not _matches(f, match):
                continue
            hit = True
            for k, v in patch.items():
                f[k] = v
                if k in _SOURCE_FIELD:
                    f[_SOURCE_FIELD[k]] = "user"
            ids = f.setdefault("applied_correction_ids", [])
            if cid not in ids:
                ids.append(cid)
        if hit:
            applied.add(cid)
    unmatched = [c for c in corrections if c["id"] not in applied]
    return facts, applied, unmatched
