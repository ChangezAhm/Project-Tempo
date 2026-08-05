"""Fixed-case benchmark for the populate pipeline (Fill-Plan migration gates).

Runs each case in cases.json through populate_from_bytes and scores the output
against (a) human-verified expected cells and (b) the stored baseline snapshot.
LLM spend is real — run at phase gates, not in CI. Source understanding is
cached by file hash, so repeat runs mostly pay for planning/mapping only.

Usage (from parser/):
    python benchmarks/run_bench.py                    # all cases, legacy path
    python benchmarks/run_bench.py --case meridian    # one case
    python benchmarks/run_bench.py --fill-plan        # TEMPO_FILL_PLAN=1 (new path)
    python benchmarks/run_bench.py --save-baseline    # overwrite baselines/<case>.json
    python benchmarks/run_bench.py --label phase1     # tag the results file

Scoring per case:
  correct        expected cell filled with the right value (rel tol 1e-6)
  incorrect      expected cell filled with a WRONG value  <- the worst outcome
  missing        expected cell not filled
  bad_fill       an expected_blank cell got a value        <- also the worst outcome
  drift          filled-per-sheet counts vs the baseline snapshot
  questions      review items / open questions produced
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH.parent))

REL_TOL = 1e-6


def _key(x: dict) -> str:
    return f"{x.get('template_sheet')}!{x.get('template_cell')}"


def _close(a, b) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)
    if a == b:
        return True
    return abs(a - b) <= REL_TOL * max(abs(a), abs(b), 1e-12)


def score(resp: dict, expected: dict | None, baseline: dict | None) -> dict:
    filled = {_key(x): x.get("value") for x in resp.get("filled", [])}
    out: dict = {
        "filled_total": len(filled),
        "unmatched_total": len(resp.get("unmatched", [])),
        "questions": len(resp.get("open_questions", []) or []) + len(resp.get("reconciled", []) or []),
    }
    by_sheet: dict[str, int] = {}
    for k in filled:
        by_sheet[k.split("!", 1)[0]] = by_sheet.get(k.split("!", 1)[0], 0) + 1
    out["filled_by_sheet"] = by_sheet

    if expected:
        correct, incorrect, missing = [], [], []
        for cell, want in (expected.get("cells") or {}).items():
            got = filled.get(cell)
            if got is None:
                missing.append(cell)
            elif _close(got, want):
                correct.append(cell)
            else:
                incorrect.append({"cell": cell, "want": want, "got": got})
        bad_fill = [c for c in (expected.get("expected_blank") or []) if c in filled]
        out.update(correct=len(correct), incorrect=incorrect, missing=missing, bad_fill=bad_fill)

    if baseline:
        base_by_sheet: dict[str, int] = {}
        for x in baseline.get("filled", []):
            sh = x.get("template_sheet")
            base_by_sheet[sh] = base_by_sheet.get(sh, 0) + 1
        out["drift_by_sheet"] = {
            sh: {"baseline": base_by_sheet.get(sh, 0), "now": by_sheet.get(sh, 0)}
            for sh in sorted(set(base_by_sheet) | set(by_sheet))
            if base_by_sheet.get(sh, 0) != by_sheet.get(sh, 0)
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default=None)
    ap.add_argument("--fill-plan", action="store_true", help="run with TEMPO_FILL_PLAN=1")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    if args.fill_plan:
        os.environ["TEMPO_FILL_PLAN"] = "1"
    os.environ.setdefault("TEMPO_MAX_RUN_USD", "15")

    from app.population.run import populate_from_bytes  # after sys.path setup

    cases = json.loads((BENCH / "cases.json").read_text(encoding="utf-8"))
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]
        if not cases:
            print(f"no case named {args.case!r}")
            return 2

    results = {}
    failures = 0
    for case in cases:
        name = case["name"]
        src = Path(case["source"])
        if not src.exists():
            print(f"[{name}] SKIP — source not found: {src}")
            continue
        expected = None
        if case.get("expected"):
            expected = json.loads((BENCH / case["expected"]).read_text(encoding="utf-8"))
        base_path = BENCH / "baselines" / f"{name}.json"
        baseline = json.loads(base_path.read_text(encoding="utf-8")) if base_path.exists() else None

        print(f"[{name}] populating {case['template_id']} "
              f"({'fill-plan' if args.fill_plan else 'legacy'} path)…")
        resp = populate_from_bytes(case["template_id"], src.name, src.read_bytes(),
                                   as_of_date=case.get("as_of_date"),
                                   reset="full", add_lines="apply")
        s = score(resp, expected, baseline)
        results[name] = s
        # full response persisted per run — diagnosis must never require a re-run
        resp_path = BENCH / "results" / f"{name}-last-{'fillplan' if args.fill_plan else 'legacy'}.json"
        resp_path.write_text(json.dumps(resp, default=str, indent=1), encoding="utf-8")
        print(f"[{name}] filled={s['filled_total']} unmatched={s['unmatched_total']} "
              f"questions={s['questions']}")
        if expected:
            print(f"[{name}] correct={s['correct']} incorrect={len(s['incorrect'])} "
                  f"missing={len(s['missing'])} bad_fill={len(s['bad_fill'])}")
            if s["incorrect"] or s["bad_fill"]:
                failures += 1
                for bad in s["incorrect"]:
                    print(f"    WRONG {bad['cell']}: want {bad['want']} got {bad['got']}")
                for c in s["bad_fill"]:
                    print(f"    BAD FILL (should be blank): {c}")
            if s["missing"]:
                print(f"    missing: {s['missing']}")
        if s.get("drift_by_sheet"):
            print(f"[{name}] drift vs baseline: {s['drift_by_sheet']}")
        if args.save_baseline:
            base_path.write_text(json.dumps(resp, default=str, indent=1), encoding="utf-8")
            print(f"[{name}] baseline saved -> {base_path.name}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = (args.label + "-" if args.label else "") + ("fillplan" if args.fill_plan else "legacy")
    out = BENCH / "results" / f"{stamp}-{label}.json"
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\nresults -> {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
