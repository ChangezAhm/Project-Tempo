"""Populate regression gate — the LIVE saas template x the Aurora data pack.

Replaces the deleted Meridian/pe-flash benchmarks with the pair we have deep
ground truth for: the 2026-07-13 saas financial model (owner-labelled, evaluated
cell-by-cell) filled from the Aurora Software monthly pack — the same run that
established the known-good baseline (126 fills incl. the revenue block, 4
placeholder KPI renames, questions within budget).

Run manually before/after engine changes (LLM mapping spend ~$1-2/run; source
understanding is content-hash cached):

    python benchmarks/run_populate_bench.py

Gates (fail loudly, no partial credit):
  fills        >= 100        (baseline 126)
  revenue      >= 6 Subscription-metric fills (the block that once shipped empty)
  questions    <= 15         (open questions + review items, the owner's cap)
  violations   == 0          rule violations
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.population.run import populate_from_bytes  # noqa: E402

TEMPLATE_ID = "a05cf69d-38c8-456b-82b9-b158d6fa97b4"   # 2026-07-13 saas model
SOURCE = Path(__file__).parent / "fixtures" / "aurora-software-financials.xlsx"


def main() -> int:
    # NOT dry_run: that flag is estimate-only (no mapping happens). A real run
    # writes a filled version + files questions — both budgeted/idempotent.
    res = populate_from_bytes(
        TEMPLATE_ID, SOURCE.name, SOURCE.read_bytes(), None)

    fills = res.get("links_count", 0)
    filled = res.get("filled") or []
    revenue = sum(1 for f in filled
                  if "subscription" in str(f.get("metric", "")).lower())
    # What the USER sees: questions actually filed to the inbox by the budget
    # gate (filed + still-open re-asked), NOT the response's internal review
    # notes (res["review"] holds per-cell audit notes, deliberately verbose).
    q = (res.get("routing") or {}).get("questions") or {}
    questions = q.get("filed", 0) + q.get("kept_open", 0)
    violations = res.get("rule_violation_count", 0)

    print(f"fills: {fills} | subscription fills: {revenue} "
          f"| inbox questions: {questions} {q} | violations: {violations}")

    gates = [
        ("fills >= 100", fills >= 100),
        ("subscription revenue fills >= 6", revenue >= 6),
        ("questions <= 15", questions <= 15),
        ("violations == 0", violations == 0),
    ]
    ok = True
    for name, passed in gates:
        print(("  PASS  " if passed else "  FAIL  ") + name)
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
