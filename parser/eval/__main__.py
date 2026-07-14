"""CLI: `python -m eval` runs the full corpus (constructed + 9 real templates),
prints a scorecard, checks invariants + baseline drift, exits non-zero on any
regression. `python -m eval --update-baseline` re-baselines the category
distributions after an intended change.

Because the real cases hit Supabase and run the deterministic derivation over
big templates, this is a dev/CI command, not part of the default pytest run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import app.config  # noqa: F401 — loads parser/.env (Supabase + keys)
from eval.corpus import CONSTRUCTED, real_cases
from eval.harness import CaseResult, run_case

_BASELINE = Path(__file__).parent / "baselines.json"
_DRIFT = 0.15   # relative category-count change that counts as drift


def _load_baseline() -> dict:
    return json.loads(_BASELINE.read_text()) if _BASELINE.exists() else {}


def _fmt_cats(cats: dict) -> str:
    return " ".join(f"{k}:{v}" for k, v in sorted(cats.items()))


def _drift(name: str, cats: dict, baseline: dict) -> list[str]:
    prev = baseline.get(name)
    if not prev:
        return []
    out = []
    for k in set(cats) | set(prev):
        a, b = prev.get(k, 0), cats.get(k, 0)
        if a == 0 and b == 0:
            continue
        base = max(a, 1)
        if abs(b - a) / base >= _DRIFT:
            out.append(f"{k} {a}->{b}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--constructed-only", action="store_true")
    args = ap.parse_args()

    cases = list(CONSTRUCTED) + ([] if args.constructed_only else real_cases())
    baseline = _load_baseline()
    results: list[CaseResult] = [run_case(c) for c in cases]

    print("\n" + "=" * 92)
    print(f"{'CASE':<30} {'FACTS':>6} {'INV':>7} {'P/R/F1':>16}  CATEGORIES / NOTES")
    print("-" * 92)
    regressed = False
    new_baseline = dict(baseline)
    for r in results:
        if not r.ran:
            print(f"{r.name:<30} {'—':>6} {'ERROR':>7}   {r.error}")
            regressed = True
            continue
        hard = [i for i in r.invariants if not i.advisory]
        n_ok = sum(1 for i in hard if i.ok)
        inv_s = f"{n_ok}/{len(hard)}" if hard else "—"
        pr = (f"{r.precision:.2f}/{r.recall:.2f}/{r.f1:.2f}"
              if r.precision is not None else "—")
        print(f"{r.name:<30} {r.n_facts:>6} {inv_s:>7} {pr:>16}  {_fmt_cats(r.categories)}")
        for i in r.invariants:
            if i.ok:
                continue
            if i.advisory:
                print(f"    ⚠ advisory {i.name}: {i.detail}")
            else:
                print(f"    ✗ INVARIANT {i.name}: {i.detail}")
                regressed = True
        drift = _drift(r.name, r.categories, baseline)
        if drift and not args.update_baseline:
            print(f"    ⚠ DRIFT vs baseline: {', '.join(drift)}")
        new_baseline[r.name] = r.categories

    # precision/recall summary over cases that have verified ground truth
    scored = [r for r in results if r.precision is not None]
    if scored:
        import statistics
        print("-" * 92)
        print(f"input-detection over {len(scored)} verified case(s): "
              f"precision={statistics.mean(r.precision for r in scored):.3f}  "
              f"recall={statistics.mean(r.recall for r in scored):.3f}  "
              f"F1={statistics.mean(r.f1 for r in scored):.3f}")
    print("=" * 92)

    if args.update_baseline:
        _BASELINE.write_text(json.dumps(new_baseline, indent=2, sort_keys=True))
        print(f"baseline updated → {_BASELINE}")
        return 0

    print("REGRESSION" if regressed else "OK — all invariants hold")
    return 1 if regressed else 0


if __name__ == "__main__":
    sys.exit(main())
