# Input-detection eval harness

Closes reliability-audit gap §7 ("we have no way to know whether input detection
improved or regressed on real templates"). Runs the **deterministic** L4
derivation (`derive_data_model` — no LLM, free, reproducible) over golden cases
and scores it.

## Run it

```bash
cd parser
python -m eval                    # full corpus (3 constructed + 9 real templates); needs Supabase creds
python -m eval --constructed-only # fast, no DB
python -m eval --update-baseline  # re-baseline category distributions after an intended change
```

Exit code is non-zero if any **hard invariant** fails or a case errors — usable
as a CI gate. The fast constructed cases also run inside `pytest`
(`tests/test_eval_harness.py`).

## What it measures

- **Hard invariants** (regression gates) — verified truths: no positional
  `row N` labels among inputs, control/selector cells excluded, real metrics stay
  fillable, connector inputs carry a period. A failure = a regression.
- **Advisory invariants** (⚠, reported not gated) — faithfulness signals such as
  totals being labelled fillable in the model though excluded downstream.
- **Precision / recall / F1** — only on cases with *verified* ground truth
  (the constructed cases today). Perfect on those (1.00).
- **Category-distribution drift** — per-template baseline in `baselines.json`; a
  silent shift (inputs lost, pollution returned) is flagged even before a
  template is hand-labelled.

## Adding a golden case

- **Constructed** (authoritative ground truth, runs in pytest): add to
  `corpus.CONSTRUCTED` using `builders.build(...)` + an `expected` map.
- **Real template**: add its id to `corpus.REAL_TEMPLATE_IDS`; give it invariants.
  To get true precision/recall on a real template it must be hand-labelled
  (expected input set) — that labelling is the next investment (audit §10-A).
