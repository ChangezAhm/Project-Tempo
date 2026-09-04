# Refactor: Population Pipeline Decomposition

**Date:** 2026-08-17 · **Trigger:** Build Audit (docs/Build-Audit — risk #3): `_run_population`
is ~1,100 lines inside a 1,651-line `run.py`, with three mapper variants, two revision loops,
~20 broad exception guards with inconsistent semantics, and **near-zero integration-test
coverage of the orchestration itself**. Failures now come from correct pieces interacting
incorrectly — the class of bug a god-function makes unreasonable to reason about.

## Goals

1. **Decompose** the orchestrator into named, individually-testable stages over an explicit
   `RunState` — no behavior change.
2. **Consolidate** the duplicated logic the monolith accumulated (three copies of the
   verify→execute block; the plan-cache key computed independently in the dry-run and live
   paths; ad-hoc exception guards).
3. **Test the wiring**: an offline integration harness that runs the whole pipeline with
   monkeypatched LLM/storage — the coverage hole the audit rated the most dangerous.
4. Fix confirmed **doc drift** (mapping.py module docstring still describes the deleted
   text-only pipeline).

## Non-negotiable invariants (behavior-preserving contract)

- Public API of `app.population.run` unchanged: `populate_from_bytes`, `populate_from_snapshot`,
  `render_filled`, `build_demand`, `_is_clearable_value`, `_pick_timeline`, `_meaningless_key`
  (tests/benchmarks/main.py import these by name).
- The result dict: same keys, same truncation limits, same `routing` entries.
- LLM call **sequence and gating** identical: one-pass gate (grids ∧ no plan-cache ∧ ≤80 metrics),
  claims quality gate (cache 1.3× / degraded fallback), plan-cache read before mapping,
  'mapped' write immediately after mapping, 'final' write after the tie-out loop,
  conservation/revision gated on `plan_stage != "final"`, deep rescue only in digest mode.
- Progress stages, spend-guard arming, run_stamp naming, cache formats and keys, review-item
  batching and priorities: unchanged.
- `SpendCapExceeded` propagates from exactly the blocks it propagates from today.

## Target structure

```
app/population/
  run.py                     # ~150 lines: entry points + re-exports (API unchanged)
  pipeline/
    __init__.py              # run_pipeline(state): the stage sequence, one try/finally
    state.py                 # RunState dataclass + ask() + best_effort() guard
    demand.py                # build_demand, torn-model guard, _meaningless_key
    prepare.py               # template load, snapshot context, check discovery,
                             # slot facts, grid + image build, temp-file helpers
    understand.py            # plan-cache read, one-pass understand+map, claims gate,
                             # catalogue fallback, plan_key_for() (single source of truth)
    plan.py                  # mapping, rescue, conservation, aggregation, contract overlay,
                             # verify_and_execute() (deduped), revision loop, tie-out loop,
                             # plan-cache writes
    report.py                # apply_links, sign checks, unmatched reasons, coverage summary,
                             # all review-question assembly
    deliver.py               # render_filled, additions routing, render+upload, template-check
                             # summary, audit upload, final result dict
    estimate.py              # dry-run consent estimate (shares plan_key_for + grid builder)
```

## Phases

**Phase 1 — mechanical decomposition (this change).** Move code blocks verbatim into stage
functions `stage_x(state: RunState) -> None`; thread every former local through `RunState`.
`run.py` keeps thin wrappers and re-exports. Full test suite must stay green, unmodified.

**Phase 2 — consolidation (this change).**
- `verify_and_execute(state)` replaces the three copied verify→repair→execute blocks
  (initial pass keeps its repair round; revision and tie-out reuse the same function without it,
  exactly as today).
- `plan_key_for(content_hash, version_id)` used by both the estimate and the live run —
  the two inline sha256 computations were one string-format edit away from a silent
  cache-miss bug.
- `best_effort(state, what)` context manager standardises the ~20 broad guards: logs, records
  into `routing["stage_warnings"]` (additive field — nothing consumed it before), re-raises
  `SpendCapExceeded` exactly where today's code does (flag for the blocks that deliberately
  swallow everything).
- try/finally widened so `tgt_tmp` also gets cleaned when a pre-mapping stage raises
  (today it leaks on a one-pass cap abort — temp-file hygiene only, no visible behavior).
- mapping.py module docstring rewritten to describe the grid/one-pass reality.

**Phase 3 — deferred (behavior-changing; needs sign-off).** Not in this change:
retire the digest mapper once the 20-pair eval exists; unify `checks.py`/`template_checks.py`;
merge the two scenario-word lexicons; move plan/understanding caches out of the OS temp dir;
queue + resumable-stage persistence; calibrate the 0.6 confidence gate against outcomes.

## New tests (the audit's biggest hole)

`tests/test_pipeline_integration.py` — the whole `_run_population` offline:
LLM entry points (`understand_and_map`, `map_metrics`, `understand_source`) and storage
(`supabase_client.*`, `get_data_model`, `load_context`, `file_questions`) monkeypatched;
source snapshot + claims fixtures reused from `test_transposed`; a real template workbook
built with Aspose in-fixture. Cases:

1. Happy path (grid one-pass): fills land, result-dict key contract holds.
2. Digest fallback path: one-pass failure → `understand_source` + `map_metrics` run.
3. Plan-cache 'final': **no** mapping/one-pass call; conservation and revision skipped.
4. 'mapped' cache write occurs immediately after fresh mapping.
5. Torn data model → `RuntimeError`, no LLM spend.
6. `dry_run` estimate: shape + `cap_exceeded`, zero LLM calls.
7. Tie-out loop: failing template check → `revise_for_checks` invoked, revised plan re-verified.
8. `SpendCapExceeded` in mapping propagates (run dies, no partial delivery).
9. A guarded stage failure (conservation raises) → run completes, `stage_warnings` records it.

## Verification protocol

`pytest -q` (413 green) before and after; grep check that no external import of
`app.population.run` symbols broke; eval invariants untouched (no derivation-side change).
