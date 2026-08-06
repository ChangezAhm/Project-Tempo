"""Template business context — the knowledge channel into mapping (Gap 3).

The system extracts business logic at onboarding (definitions, qualification
criteria, author rules) and collects human knowledge over time (contract
notes, answered review questions) — but until now NONE of it reached the one
decision-maker that needed it: the metric→series mapper. This module builds a
compact, prioritised context block that rides into the mapping prompt:

  1. contract notes            — the user's own words about this template
  2. answered review questions — knowledge the inbox captured (Q + A)
  3. strict workbook rules     — is_strict author/business rules from L3

Capped hard (it accompanies every mapping batch), highest-value first. Pure
assembly is separated from I/O so it's testable offline; every loader source
is best-effort — missing context must never block a populate.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import os as _os

# Rides on every mapping batch. 2,400 chars was capping how much of the user's
# OWN confirmed guidance reached the matcher — raised to 6,000, env-tunable.
try:
    _MAX_CHARS = int(_os.environ.get("TEMPO_CONTEXT_MAX_CHARS", "6000"))
except ValueError:
    _MAX_CHARS = 6000
_MAX_ITEMS_PER_SOURCE = 16


def usage_brief(wb: dict | None) -> str | None:
    """One-paragraph 'what this workbook IS and how it is used' from the stored
    workbook understanding — so downstream judgments (mapping, enrichment) know
    they are filling a TEMPLATE on a portco's behalf, not reading a report.
    Pure; None when nothing useful is stored."""
    if not wb:
        return None
    bits = []
    if wb.get("archetype"):
        bits.append(str(wb["archetype"])[:160])
    if wb.get("purpose"):
        bits.append(str(wb["purpose"])[:240])
    surface = wb.get("input_surface_sheets") or []
    if surface:
        bits.append("Data entry happens on: " + ", ".join(map(str, surface[:10])))
    if not bits:
        return None
    return ("This workbook is a TEMPLATE our system populates on behalf of a "
            "portfolio company. " + " — ".join(bits))


def build_context(notes: str | None, answered: list[dict], strict_rules: list[dict],
                  brief: str | None = None, max_chars: int = _MAX_CHARS) -> str:
    """Assemble the context block. Pure — takes already-loaded rows."""
    lines: list[str] = []
    if brief:
        lines.append("TEMPLATE USAGE:")
        lines.append(f"  {brief[:500]}")
    if notes and notes.strip():
        lines.append("SPONSOR NOTES:")
        lines.append(f"  {notes.strip()[:600]}")
    if answered:
        lines.append("CONFIRMED ANSWERS (a human settled these):")
        for it in answered[:_MAX_ITEMS_PER_SOURCE]:
            q = (it.get("question") or "").strip()
            a = ((it.get("resolution") or {}).get("answer") or "").strip()
            if q and a:
                lines.append(f"  Q: {q[:160]}")
                lines.append(f"  A: {a[:200]}")
    if strict_rules:
        lines.append("TEMPLATE RULES (author-stated, strict):")
        for r in strict_rules[:_MAX_ITEMS_PER_SOURCE]:
            desc = (r.get("description") or r.get("summary") or r.get("raw_text") or "").strip()
            cat = r.get("category") or r.get("rule_category") or ""
            if desc:
                lines.append(f"  - [{cat}] {desc[:180]}")
    return "\n".join(lines)[:max_chars]


def load_context(template_id: str, version_id: str) -> str:
    """I/O wrapper: gather the three sources and assemble. Each source is
    best-effort — a read failure degrades to less context, never an error."""
    from app import supabase_client as sb

    notes = None
    try:
        contract = sb.get_contract(template_id)
        notes = (contract or {}).get("notes")
    except Exception as e:  # noqa: BLE001
        logger.info("contract notes unavailable for context (%s)", e)

    answered: list[dict] = []
    try:
        answered = [i for i in sb.list_review_items(version_id)
                    if i.get("status") == "answered"]
    except Exception as e:  # noqa: BLE001
        logger.info("review answers unavailable for context (%s)", e)

    strict_rules: list[dict] = []
    brief = None
    try:
        row = (sb.get_client().table("template_understanding")
               .select("understanding")
               .eq("template_version_id", version_id).limit(1).execute().data)
        wb = (row[0].get("understanding") or {}) if row else {}
        strict_rules = [r for r in (wb.get("business_rules") or []) if r.get("is_strict")]
        brief = usage_brief(wb)
    except Exception as e:  # noqa: BLE001
        logger.info("workbook rules unavailable for context (%s)", e)

    return build_context(notes, answered, strict_rules, brief=brief)
