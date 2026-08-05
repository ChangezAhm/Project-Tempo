"""Shared PRIOR lexicons — defaults about author intent, consolidated in one place.

Per the authority model (docs/Fill-Plan-Architecture.md §1) these are PRIORS:
advisory, tie-breaking knowledge about how template authors conventionally name
things — never silently authoritative. Every user of a prior must keep its
decision visible and overridable (review-flagged categories, corroboration of a
model claim, candidate ORDERING — never a silent filter or an unlogged gate).

Two strictness tiers, matched to what each caller's mistake costs:
  - STRICT banks (is_placeholder_label, ADJUSTMENT_MEMBER_STRICT) drive
    classification with real consequences — the data model dropping a line from
    populate demand, a region making an occupied label overwritable. A bare
    "Other" P&L line or a "Transaction fees" revenue block must never match.
  - LOOSE banks (is_placeholder_slot_label, ADJUSTMENT_LEXICON) serve callers
    that only corroborate a model claim alongside structural signals, or only
    ORDER candidates — there a wider net is safe and useful.
This module imports nothing from app — stdlib only.
"""

from __future__ import annotations

import re

# --- placeholder labels -------------------------------------------------------
# A PLACEHOLDER label reads as a THROWAWAY slot name rather than a real business
# line: the empty, generically-named slots of an extensible area ("KPI Label 3",
# "Custom Metric Amount 1", "Adjustment 3", "[Specify]", "Other…"). The data
# model routes these to category 'config' (the additions/region path fills them,
# not populate); region authoring uses them to corroborate a model 'placeholder'
# claim — text judgment alone never makes a real label overwritable.
_PLACEHOLDER_STRICT_RES = [
    re.compile(r"^\[.*\]\s*$"),                                          # [Specify]
    re.compile(r"(?i)^(?:custom\s+)?(?:kpi|metric|line\s*item|item)(?:\s+(?:label|amount|name))?\s*#?\d+(?:\s*\[[^\]]*\])?\s*$"),
    re.compile(r"(?i)^(?:kpi|metric|line|item|label)\s+label\s*#?\d+\s*$"),   # 'KPI Label 1'
    re.compile(r"(?i)\blabel\s*#?\d+\s*$"),                                   # '… - Label 2'
    re.compile(r"(?i)^(adjustment|add-?back|item|line|metric|kpi)\s*#?\d+[:.\s]*$"),  # Adjustment 3
    # the noun group is REQUIRED here: a bare "Other"/"New"/"Additional" is a
    # real P&L line far more often than a slot name (see the loose tier below)
    re.compile(r"(?i)^(custom|other|new|additional)\s+(kpi|metric|item|line|row|adjustment)s?\s*#?\d*[:.\s]*$"),
    re.compile(r"(?i)^(please\s+)?specify\b"),
    re.compile(r"(?i)^add\s+(a\s+)?(kpi|line|metric|item|row)\b"),
    re.compile(r"(?i)^(?:tbd|n/?a|placeholder|xxx+|-+)\s*:?\s*$"),
    re.compile(r"…\s*$"),                                                # trailing ellipsis ("Other…")
]

# The loose extension: bare generic words with no slot noun ("Other", "Others",
# "New", "Additional 2"). Safe only where structural signals corroborate.
_PLACEHOLDER_SLOT_EXTRA_RES = [
    re.compile(r"(?i)^(custom|other|new|additional)\s*(kpi|metric|item|line|row|adjustment)?s?\s*#?\d*[:.\s]*$"),
]


def is_placeholder_label(text: str | None) -> bool:
    """STRICT prior: does this label read as a throwaway slot name? Used by the
    data model's category classification (a hit drops the cell from populate
    demand), so bare "Other"/"New"/"Additional" deliberately do NOT match."""
    t = (text or "").strip()
    return bool(t) and any(rx.search(t) for rx in _PLACEHOLDER_STRICT_RES)


def is_placeholder_slot_label(text: str | None) -> bool:
    """LOOSE superset of is_placeholder_label (adds bare "Other"/"New"/…). For
    region authoring, whose call sites only corroborate a model 'placeholder'
    claim alongside structural signals — never a silent classifier."""
    t = (text or "").strip()
    return bool(t) and any(rx.search(t) for rx in
                           _PLACEHOLDER_STRICT_RES + _PLACEHOLDER_SLOT_EXTRA_RES)


# --- earnings-adjustment lexicon ----------------------------------------------
# MEMBER labels of an EBITDA-adjustment / normalisation / one-off list. The
# BROAD bank ORDERS candidates for adjustment regions (region_bridge — ranking
# only, it can't filter anything out, so bare 'transaction'/'adjust' are fine).
ADJUSTMENT_LEXICON = re.compile(
    r"(?i)add.?back|one.?off|one.?time|exceptional|non.?recurring|normali[sz]|"
    r"pro.?forma|run.?rate|restructur|redundanc|transaction|integration|"
    r"management fee|monitoring fee|stock comp|share.?based|deal cost|"
    r"separation cost|impair|write.?off|write.?down|provision|litigation|"
    r"earn.?out|\bM&A\b|adjust")

# The STRICT member bank corroborates that a roll-up block IS an adjustment list
# (making its rows editable — an overwrite path), so the generic words need
# their qualifier: 'transaction cost' / 'integration cost', never a bare
# 'transaction' that would swallow a "Transaction fees" revenue block.
ADJUSTMENT_MEMBER_STRICT = re.compile(
    r"(?i)(add-?back|one-?off|exceptional|non-?recurring|restructur|redundanc|"
    r"deal cost|transaction cost|share-?based|management fee|monitoring fee|"
    r"impair|write-?off|write-?down|run-?rate|normalis|integration cost|"
    r"separation cost|provision|litigation|earn-?out|\bM&A\b)")

# A subtotal LABEL that announces a normalisation / adjustment roll-up.
ADJUSTMENT_SUBTOTAL = re.compile(r"(?i)\b(adjust|normali[sz]|pro[- ]?forma|underlying|bridge)\b")
