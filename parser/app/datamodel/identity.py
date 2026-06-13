"""Content-addressed identity for a fact.

A correction must re-bind to the right fact even when cells move between
template versions, so the key is the *natural coordinate tuple*, not the cell
address. Built from the sheet role (more stable than sheet name), the metric
identity (canonical name if present, else a slug of the label), the period
identity (parsed date if present, else a slug of the label), and the
scenario / basis / entity.
"""

from __future__ import annotations

import hashlib
import re


def slug(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:48]


def fact_key(
    *,
    sheet_role: str | None,
    metric: str | None,
    period: str | None,
    scenario: str,
    basis: str,
    entity: str | None,
) -> str:
    raw = "|".join([
        slug(sheet_role) or "?",
        slug(metric) or "?",
        slug(period) or "noperiod",
        scenario,
        basis,
        slug(entity) or "default",
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
