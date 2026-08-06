# Fill-Plan Architecture — Migration Plan

**Date:** 5 August 2026
**Status:** ALL PHASES (0–4) DELIVERED (2026-08-05, 340 tests green, live gates passed — see §11–§13). The Fill-Plan path is the only populate path; lexicon categories are overridable priors; contract decisions carry scope. **User action pending: apply `supabase/migrations/0011_category_source.sql`** (a safety fallback keeps derivation working until then). This document is the canonical plan; the Word rendering (`Tempo Fill-Plan Plan.docx`) is a reading copy.
**Prerequisite reading:** `Tempo System Diagnosis.docx` (the audit this plan answers) and `docs/SYSTEM_AUDIT.md`.

---

## 0. Objective

Rebuild the populate pipeline around a single governing principle:

> **The LLM proposes. Deterministic code verifies and executes. The user resolves genuine intent. The contract remembers.**

Concretely: replace the ~140 global semantic heuristics ("Kind-3 rules") that currently decide meaning with (a) a complete, typed, series-level **Fill Plan** produced by the LLM, (b) a deterministic **verifier/executor** that type-checks and runs the plan without re-deciding it, (c) a **repair → default → question** resolution ladder for everything the verifier rejects, and (d) a **scoped Template Contract** that makes every confirmed answer replay deterministically forever.

The end state is a *smaller* codebase than today's, with every LLM call traced in LangSmith through one choke point.

### Non-negotiables (red lines that survive every phase)

1. **The LLM never produces a numeric value that gets written.** It cites cells and declares operations; the executor reads and calculates. (This is the lesson of the hallucinating vision-matcher — permanent.)
2. **Trust-first constraints stay:** no partial aggregates written, no template totals/subtotals/headers overwritten, no double-counting one source amount into the same total, no formula/error strings written, spend caps on every call.
3. **Standing owner decisions stay:** no FX conversion (passive `currency_unverified` note only); explicit-dates-only (no wall-clock defaults in populate); the template's own timeline is honored as-is.
4. **Every claim traceable:** every written value cites real source cells; derived values show their arithmetic.

---

## 1. The authority model

Every piece of decision-making logic in the parser must be classifiable into exactly one of five kinds, each with a defined authority level. This taxonomy is the review standard for all future changes.

| Kind | Definition | Authority | Examples |
|---|---|---|---|
| **Fact** | Objective data read from the workbook | Absolute — nothing overrides a fact | Cell values, cached formula results, number formats, dates, dependency graph, protection/colors |
| **Constraint** | A safety invariant whose violation is never acceptable | Absolute — blocks execution, produces a structured error | No partial quarter-sums; no double-counting; % never scaled into money; totals never written |
| **Prior** | A reasonable default about author intent | **Advisory only** — fed to the planner as a suggestion, or used as a tier-2 default *with a visible flag*; never silently decides | "Headcount is usually period-end"; "a % metric usually averages"; "these colors usually mean input" |
| **Intent judgment** | What the author/user actually means | **The LLM's** (grounded in facts), then the user's | Metric identity, roll-up semantics, scenario intent, unit reading, sign convention |
| **Learned decision** | A confirmed answer with a scope | Absolute *within its scope* — replays deterministically, suppresses re-asking | "This template's quarterly headcount uses quarter-end"; "row 'Turnover – North' feeds Revenue" |

**The core defect being fixed:** priors and intent judgments were implemented as constraints. The regex `headcount|fte` does not *know* a row is a period-end stock — it guesses, with absolute authority. Every migration step below is one of three moves: **delete** a fake constraint, **demote** it to a prior, or **move** it into the verifier as a real constraint.

---

## 2. Target architecture

Five components. Populate data flow left to right:

```
┌──────────┐   ┌────────────────┐   ┌──────────────┐   ┌───────────────┐   ┌──────────┐
│ PARSER    │→ │ PLANNER (LLM)  │→ │ VERIFIER      │→ │ EXECUTOR       │→ │ CONTRACT │
│ facts     │   │ Fill Plan      │   │ type-checker  │   │ expand + write │   │ remembers│
└──────────┘   └────────────────┘   └──────┬───────┘   └───────────────┘   └──────────┘
                        ↑    ┌─ repair ────┘ structured errors
                        └────┤  (1 round, failing fills only)
                             └─ tier-2 defaults (flagged) ─ tier-3 questions (batched)
```

### 2.1 Parser (deterministic — exists, mostly unchanged)

Everything that reads facts stays: Aspose extraction, cached-value reading, the formula/precedent graph, number-format parsing, the CX_GET self-description, source understanding's deterministic scaffolding, date parsing (consolidated — see §8). Source understanding (LLM reading source structure) also stays as-is; it already follows the propose/verify pattern.

### 2.2 Planner (LLM — evolves `mapping.py` + `rescue.py`)

The current mapper answers "which series means this metric?" The planner answers the **complete** semantic question, at the **series level** (never per-cell — per-cell mapping is reserved for exceptional layouts and must be justified in the plan):

**`SeriesFill` schema (v1)** — one entry per (template metric × demanded scenario):

| Field | Meaning | Notes |
|---|---|---|
| `metric` | demand key | echoed; caller's key wins (as rescue does today) |
| `status` | `direct \| aggregate \| reconcile \| needs_decision \| unavailable` | existing taxonomy, unchanged — it works |
| `source_series` / `also_series` | catalogue ids | verifier checks existence, sheet coherence |
| `assumption` | plain-English, for reconcile | existing |
| `scenario` | `actual \| budget \| forecast` | which demanded scenario this fill serves |
| `rollup` | `end \| sum \| avg \| none` | metric-intrinsic (stock/flow/rate); executor applies it wherever grains differ, per sheet |
| `source_unit` / `target_unit` | the units **as the planner reads them** ("USD'000", "EUR m") | the executor computes the scale factor from these — the model never does arithmetic |
| `sign_flip` + `sign_basis` | direction + one-line why | verifier cross-checks against template evidence |
| `period_map` | `calendar \| positional` | positional only when the facts show a dateless side; today this is a silent fallback — it becomes a declared, flagged decision |
| `confidence` | honest [0,1] | drives the question tier, never a silent kill |

**Planner input** (per batch, all facts + priors clearly labeled):
- Source series lines: label, declared unit, sample values, period columns with dates/grain/scenario tags (already available in the catalogue).
- Template metric lines: label, unit + cell number formats, per-sheet slot grains and date ranges, existing row magnitudes (summary), L3 definition/qualification/sign-convention prose, section context ("sits in a balance-sheet section") — **as evidence, not as a decided basis**.
- **Contract decisions** (authoritative — must be followed; existing `context.py` channel, extended).
- **Priors** (clearly labeled as suggestions: "typical treatments: headcount → period-end; percentages → average; …").

Prompt-richness principle (owner decision): *if the model needs more context to decide correctly, give it more context.* Token cost is subordinate to reliability. Batch size drops from 25 to ~12 to make room; the run spend cap still governs.

Rescue survives as "plan the unplanned" — same schema, one focused agent per unresolved metric.

**Sampling:** planner calls pinned to `temperature=0` for run-to-run stability (measured by the benchmark, §10).

### 2.3 Verifier (deterministic — distilled from `binding.py`)

The verifier **never re-decides semantics**. It checks the plan against facts and constraints and returns **structured errors**, each carrying `fill`, `code`, `detail`, `severity`, and where possible a `suggested_resolution`:

| Code | Check | Tier on failure (§2.4) |
|---|---|---|
| `SERIES_NOT_FOUND` | cited series exists in the catalogue | repair |
| `PLAN_INCOMPLETE` | every demanded metric has a fill entry | repair |
| `COMPONENT_MISMATCH` | aggregate components share the primary's sheet/periods | repair (today: silently dropped) |
| `SCENARIO_NO_SOURCE` | demanded scenario has qualifying columns/variants | repair → question |
| `GRAIN_UNBRIDGEABLE` | grains differ but `rollup=none` | repair |
| `BUCKET_INCOMPLETE` | sum/avg roll-up bucket is complete (3/12 months) | default: leave blank + flag → question ("sum the 2 months anyway?") |
| `PERIOD_END_MISSING` | `end` roll-up's bucket-end month exists | default: leave blank + flag → question |
| `SCALE_CONFLICT` | scale from declared units reconciles with row magnitudes (where the template has numbers) | strong magnitude evidence → default to magnitude + flag; else question |
| `UNIT_KIND_MISMATCH` | % vs money category error | hard block (constraint) |
| `SIGN_CONFLICT` | declared sign vs dominant sign of existing template row values | strong evidence → default to evidence + flag; else question |
| `DOUBLE_COUNT` | one source amount into two lines under one total (formula-graph scoped, as today) | repair → question — **never** a default |
| `TARGET_PROTECTED` | total/subtotal/header or non-writable cell | hard block (constraint) |
| `SOURCE_CELL_INVALID` | cited cell empty / formula-string / error | hard block for that cell |
| `LOW_CONFIDENCE` | direct/aggregate below threshold (0.6) | question with the plan's own proposal as suggested answer — **not a silent blank** |

The magnitude-reconciliation and dominant-sign machinery survive — **demoted from decider to evidence**. Disagreement becomes a visible conflict instead of a silent override.

### 2.4 Resolution ladder (replaces "kill = blank")

1. **Self-repair (automatic, 1 round):** the planner receives its own plan + the structured errors for the failing fills only, and returns revisions. Re-verify. Cheap (small batch), capped.
2. **Safe default + flag:** for codes with a defined high-confidence default (table above), apply it and mark the fill `[auto-resolved: …]` — visible in the audit and the review pane, never silent.
3. **User question (batched by decision, not by cell):** one review item per (metric × issue) covering *all* affected periods — "For the 4 quarterly Headcount cells: monthly source → use quarter-end (suggested) or average?" One tap. Existing `make_item`/`insert_review_items`/contract-page infrastructure carries this; the only new part is wiring verifier residuals into it (today they die as JSON strings — the diagnosis's biggest dead-end finding).
4. **Hard block:** constraints. The cell stays blank and the reason says exactly why, but these are rare and boring (protected cells, error strings), not semantic.

A failure to *file* a question is itself surfaced (today it's a swallowed log line).

### 2.5 Executor (deterministic — distilled from `periods.pick_columns` + `apply.py`)

Given a verified plan, execution is mechanical: expand each SeriesFill across the template slots (calendar bucketing, roll-up expansion, positional alignment where declared), read every cited cell's cached value, compute `value = raw × scale × sign`, write, recalculate, read the template's own check cells. `pick_columns` (post-rollup work from Aug 5) is already 90% pure execution machinery — it loses its judgment branches and keeps its math. The write path's silent gates get instrumented (missing sheet, duplicate link → recorded, not swallowed).

### 2.6 Template Contract (learned determinism — extends the existing corrections/review-items store)

Every confirmed answer persists as a **decision record**:

```
{ scope, key, decision, provenance, source_item }
  scope ∈ { run          — one-off exception, not persisted
          , metric       — this template + this metric        (v1)
          , template     — this template                      (v1)
          , source_format— this template + this source family (v2)
          , global       — promoted MANUALLY only, with multi-template evidence }
```

Applied at three points: (a) compiled into the planner's authoritative context block; (b) mechanically-meaningful decisions (roll-up, scale, mapping) **overlay the plan before verification** — the plan cannot contradict a confirmed decision; (c) the content-addressed `item_key` (existing) suppresses re-asking. Scope discipline matters: one user confirming "headcount = period-end" must never create a global rule — global promotion is a deliberate act requiring repeated confirmations across templates.

**This contract is the deterministic codebase the product actually wants** — built from evidence of user intent, replacing the one built from debugging sessions. Second-month re-run of the same template + source family should produce zero new questions and a byte-stable plan.

---

## 3. LLM infrastructure (owner requirement: LangSmith on every call, slick plumbing)

Today: `guarded_stream` is the intended choke point but **three call sites bypass it** (`per_sheet._call`, `workbook._call_synth`, `dimensions_llm._call`), hand-rolling the guard; LangSmith wrapping is a silent best-effort (`wrap_anthropic` if importable); one path can swallow a spend-cap abort; the region detector arms the wrong cap; a Haiku tier exists that nothing uses.

Target — **`llm.py` is the only file that imports the Anthropic SDK**, and `guarded_stream` is the only way to call a model:

1. All 9 call sites route through `guarded_stream`. It owns: spend-guard check/record, model tier + thinking policy, `max_tokens`-truncation raise, temperature, corrective-retry protocol, and structured-output helpers.
2. **LangSmith:** `wrap_anthropic` stays, but (a) startup logs LOUDLY if `LANGSMITH_TRACING=true` and the package is missing; (b) every call passes `metadata` — call-site name, template_id, run/populate id, phase — so traces are navigable per run; (c) a post-run assertion in the benchmark harness: traced-call count == attempted-call count.
3. Fix the two cap bugs (triage swallowing `SpendCapExceeded`; regions arming the populate cap inside onboarding). Delete the dead `MODEL_ROUTE` tier.
4. Prompts live next to their call site with a one-line contract comment stating what the call may and may not decide.

---

## 4. Rule disposition table

The consequential Kind-3 rules and their fate. **D** = delete, **P** = demote to prior (planner hint / tier-2 default), **C** = keep as constraint, **M** = move into verifier as evidence/check, **K** = consolidate. Phase = when.

| Rule (today) | Location | Fate | Phase |
|---|---|---|---|
| Roll-up heuristic cascade (basis-overrides-LLM, %→avg, count→end) | `binding.py:273-286` | **D** — planner declares `rollup`; basis/section become planner *input*; heuristics become listed priors in the prompt | 1 |
| Confidence floor = silent blank | `binding.py:221-224` | **D** as kill; becomes `LOW_CONFIDENCE` → question with suggested answer | 1 |
| Sign: silent override of the LLM | `binding.py:337-347` | **M** — dominant-sign stays as *evidence*; disagreement = `SIGN_CONFLICT` (default-with-flag or question) | 1 |
| Scenario demand-gating judgment | `binding.py:50-60, 246-263` | **D** — planner declares scenario per fill; executor filters by tags (facts); miss = `SCENARIO_NO_SOURCE` | 1 |
| Cross-sheet aggregate components silently dropped | `binding.py:235-238` | **M** — `COMPONENT_MISMATCH`, visible | 1 |
| Positional alignment as silent fallback | `periods.py:172-177` | **M** — planner declares `period_map=positional`; executor executes it; always flagged | 1 |
| "Leading word wins" sign-prose parse | `binding.py:78-87` | **P** — the prose goes to the planner verbatim; the parse survives only as verifier evidence | 1 |
| Modal fallback scale (whole-file guess on one row) | `binding.py:182-191` | **D** — no declared unit + no magnitude = `SCALE_CONFLICT` question | 2 |
| `is_count_like` scale bypass + rollup | `units.py:49-55`, `binding.py:317-321` | **D** from decision path; **P** as prompt prior | 2 |
| Magnitude-reconciliation as silent unit override | `units.py:160-165` | **M** — evidence in `SCALE_CONFLICT`; strong evidence auto-resolves *with flag* | 2 |
| Demand-grain modal fallbacks | `periods.py:185-191`, `run.py:105-107` | **D** — grains come from facts (dates) or the planner | 2 |
| 3-currency lexicons (×4 copies) | units/catalogue/derive/numfmt | **K** into `units.py`; currency is read by source understanding + formats anyway | 2 |
| Date parsing (×3 implementations) | periods / temporal_analyzer / derive | **K** into one module (facts — keep, single implementation) | 2 |
| Period alignment (×2: binder + authoring) | `periods.py` / `authoring.py:49-86` | **K** — authoring uses the executor | 2 |
| Densest-8-sheets source selection | `source_understanding.py:121-129` | **P** — ranking stays; exclusions become a visible, question-able note | 2 |
| Complete-bucket (3/12 months) | `periods.py` | **C** — but failure becomes `BUCKET_INCOMPLETE` question, not a dead string | 1 |
| Double-count guard (formula-graph scoped) | `binding.py:150-172`, `aggregation.py` | **C** — verifier check `DOUBLE_COUNT` | 1 |
| Partial-sum refusal; formula-string block; total/subtotal guard; %-vs-money block; serial window | apply/binding/units/periods | **C** — unchanged | — |
| Category cascade label lexicons (junk/control/placeholder) | `derive.py:165-215, 356-387` | **D/P** — LLM input-identification grounded on facts (formula→computed and connector→sourced stay as facts); lexicons become priors | 3 |
| 11-color input palette | `cell_analyzer.py:17-29` | **P** — colors are facts shown to the model; the palette becomes a routing hint | 3 |
| Adjustment/placeholder lexicons (×2 each) | `regions.py`, `region_bridge.py` | **K** + **P** — one copy, ranking-hint only (structural vetting stays as constraint) | 3 |
| Contradictory "is 0 empty" rules | `prompts.py:162` vs `derive.py:845` | **K** — one rule (0 is a value), stated once | 3 |
| Dead: `MODEL_ROUTE`; unread source `sign_flip`; dead `TEMPO_MAX_DEEP_SHEETS` path | various | **D** | 0–2 |

Target: **~140 encoded judgments → under 40**, every survivor classified as C (constraint) or P (prior) in a rule ledger (§9).

---

## 5. Phased delivery

Each phase has a gate: the benchmark (§10) must not regress, and the phase's deletions must have landed (a phase is not done while its dead code lives).

### Phase 0 — Freeze, plumbing, baseline (small, immediate)

- **Rule freeze** (policy, in the repo's CLAUDE.md): no new Kind-3 rules. A failure's first question is "which planner field or verifier error should have owned this?"
- LLM plumbing consolidation (§3): one choke point, LangSmith metadata, cap-bug fixes, dead-tier deletion, `temperature=0` for mapping/planning calls.
- Instrument the silent gates (missing-sheet write, duplicate link, dropped components) — one-line records, no behavior change.
- Build the **benchmark harness** + fixtures from verified runs (§10). Record baseline numbers for the current system.

### Phase 1 — The vertical slice (prove the architecture)

Scope: the failure we understand completely — **Aurora monthly → Meridian quarterly KPI (ARR, Headcount, Churn)** — running end-to-end through the new path, behind a temporary `TEMPO_FILL_PLAN=1` flag (the flag dies in Phase 2; no parallel paths linger).

1. `SeriesFill` schema + planner prompt (evolve `mapping.py`'s status taxonomy; add the fields in §2.2; priors labeled as suggestions).
2. Split `binding.py` → `verify.py` (structured errors, §2.3 subset: the period/rollup/scenario/confidence/sign codes) + `execute.py` (expansion + apply). Delete the Phase-1 rules in §4.
3. Repair loop (1 round, failing fills only).
4. Verifier residuals → **batched review items** with suggested answers (the dead-end fix).
5. Contract v1: answered item → decision record (scopes: metric, template) → planner context + pre-verify overlay.
6. Fill Plan JSON persisted with the run (the audit upload) — the plan **is** the shown working.

**Gate:** KPI slice fills correctly with the heuristic cascade deleted; a *new month's* Aurora file replays with zero new questions and a stable plan; Dream Games + PE-flash benchmarks don't regress; every call visible in LangSmith.

### Phase 2 — Full populate migration

- All metrics through the planner; rescue merged into the same schema; flag removed; old `bind()` deleted.
- Scale via declared units + magnitude cross-check (`SCALE_CONFLICT` model); Phase-2 deletions and consolidations from §4 (lexicon merges, date-parser merge, authoring aligner merge, dead code).
- Question batching polish; `unused_source_series` becomes a question-able "did I miss something?" surface instead of a dead list.

**Gate:** benchmark equal-or-better on every case; encoded-judgment count measurably down (rule ledger); net LOC in `app/population` down vs the Phase-0 baseline.

### Phase 3 — Onboarding/demand side

The derive category cascade becomes LLM input-identification grounded on facts (the original `llm-owns-meaning` fix direction): the model judges "is this a replaceable input, and what does it mean?" per region, with formula→computed and connector→sourced remaining factual. Color palettes and label lexicons demote to priors. Region detection keeps its (good) propose/verify shape. One "is 0 empty" rule.

**Gate:** owner's ground-truth labels in "Templates for testing/" (the 1/2 marking scheme) — classification accuracy vs today's cascade; no populate regression.

### Phase 4 — Contract depth + final slim-down

- `source_format` scope (decisions keyed to a source family, so a new month's pack from the same system replays everything).
- Global-promotion workflow (manual, evidence-listed).
- Final bloat audit vs the Phase-0 baseline; delete anything the migration orphaned.

---

## 6. What is deliberately NOT changing

- The **status taxonomy** (direct/aggregate/reconcile/needs_decision/unavailable) — it already encodes propose-vs-ask correctly.
- **Source understanding** and **workbook synthesis + graph verification** — they already follow propose/verify.
- The review-items store, contract page, answered-items channel — they are the Q&A backbone; we widen their funnel, not rebuild them.
- The facts layer (Aspose parse, snapshots, dependency graph, CX self-description) — the system's genuinely good foundation.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Plausible-but-wrong numbers** (the worst failure — worse than blanks) | Verifier cross-checks (magnitude, sign evidence, post-fill template checks) stay; benchmark explicitly counts *incorrect writes* and *silently wrong mappings*, not just fills; flags stay visible in review |
| Planner inconsistency across batches (same series treated differently) | Series-level plan (one decision per metric); verifier consistency check (same source series → same unit/scale everywhere); temperature 0 |
| More LLM cost/latency | Planner ≈2–3× today's mapping spend (~$2–5/run, inside the $15 cap); owner's standing decision: reliability > cost; dry-run estimates already exist |
| Regression during migration | Phase gates on a fixed benchmark; Phase-1 flag isolates the slice; no long-lived parallel paths |
| Question fatigue | Three-tier ladder; batching by decision; contract suppression; benchmark tracks questions-per-run as a first-class metric |
| "The audit blamed rules, but model errors may hide" (owner's caveat) | The benchmark inspects *correct* writes too, not only failures; verified-run fixtures catch a model that passes checks while wrong |

---

## 8. Anti-bloat rules of engagement (permanent)

1. **One implementation per concern:** dates parse in one module; units/currencies in one; all LLM calls in `llm.py`; one period aligner.
2. **The rule ledger** (`docs/RULE_LEDGER.md`, created in Phase 1): every surviving non-fact rule listed with its kind (C or P), its owner file, and why it may exist. A PR adding a rule must add a ledger row; review rejects new intent-judgments-as-code.
3. **Deletion is part of done:** each phase's gate includes its deletions. No flag outlives its phase.
4. **Questions and priors over patches:** a failing file produces (in order of preference) a contract decision, a prompt/prior improvement, a new verifier error code — a new global rule only with multi-template evidence.
5. **Metrics kept honest:** encoded-judgment count and `app/population` LOC tracked at every phase gate against the Phase-0 baseline.

---

## 9. Benchmark & evaluation protocol (Phase 0 deliverable)

`parser/benchmarks/` — a fixed case set, run manually at each phase gate (LLM spend is real; source understanding is cached by file hash so re-runs are cheap):

| Case | Why it's in the set |
|---|---|
| Aurora → Meridian (`681f6d92`) | the vertical slice: monthly→quarterly KPI, scale from 'USD'000', scenario tags |
| Aurora → PE flash (`ca1db430`) | the year-mismatch template (2025 template vs 2026 source) — tests honest blanks + questions |
| Dream Games → `94655f87` | the large verified run (501 fills) — regression anchor |
| Chronograph Rolling Monthly | connector/transposed grids — the derive side (Phase 3 gate) |
| Helios Monthly Management Pack | the messy-GL shape |

**Metrics per run:** correct writes (vs fixture), incorrect writes, unjustified blanks, justified blanks, silently-wrong mappings (fixture spot-checks), questions asked (count + quality), run-to-run stability (plan diff across two identical runs), cost, wall time, LangSmith trace completeness.

Fixtures: expected-fill JSONs built from today's verified runs plus manual spot-labels; extended whenever a run is human-verified. The owner's 1/2 ground-truth labels in "Templates for testing/" serve the Phase-3 gate.

---

## 10. Immediate next actions

Phase 2 (full migration + deletions) is next. Nothing else until its gate passes.

---

## 11. Phase 0 + 1 delivery record (2026-08-05)

**Phase 0 (done):** every LLM call routes through `llm.py::guarded_stream` (site names + run metadata into LangSmith; loud warning when tracing is requested but unavailable; `temperature=0` on planner/rescue); dead Haiku tier deleted; triage no longer swallows `SpendCapExceeded`; region detection arms the onboarding cap; missing-sheet writes and duplicate links are recorded, not silent; rule-freeze policy in `AGENTS.md`; `benchmarks/` harness with verified Meridian fixtures + legacy baselines.

**Phase 1 (done):** `SeriesFill` fields on the mapping schema; planner prompt owns the complete fill semantics (rollup, units-as-read, sign+basis, scenario, period_map) with priors clearly labeled as suggestions and per-metric SLOT FACTS attached; `periods.align_slot` exposes typed failure reasons; `verify.py` (typed PlanIssues + blocking policy), `execute.py` (mechanical expansion; declared-unit scale cross-checked by magnitudes; sign cross-checked by template evidence — conflicts auto-resolve WITH flags or become questions), `contract.py` (answered questions → decisions → plan overlay); run wiring behind `TEMPO_FILL_PLAN=1` with one repair round and batched question filing; plan + issues persisted in response and audit. Deleted from the new path: the rollup heuristic cascade, count-label scale bypass, modal fallback scale, confidence-floor-as-blank.

**Gate results:**
- Meridian (the slice): **113 filled / 9-of-9 verified cells correct / 0 incorrect / 0 bad fills** — exact parity with the legacy baseline, through plan→verify→execute with zero heuristics. Fills byte-stable across repeat runs (mapper status jitter ±1 question; same content-addressed items re-upsert, so no question accumulation).
- PE-flash: unmatched parity; 36 fewer fills, all accounted for: 12 were legacy writing raw revenue into "Revenue per Employee ($K)" (~2x wrong — the planner now correctly says `unavailable`), 24 were legacy's modal-scale guess (now one `SCALE_CONFLICT` question whose answer will refill them via the contract).
- Live corrections found at the gate (both design fixes, not new rules): `SCENARIO_NO_SOURCE` demoted to a per-slot, non-blocking data gap (blocking the whole metric held 91 actual cells hostage) and collapsed to ONE batched question per source.

**Deferred to Phase 2:** per-scenario plan entries (executor still resolves budget/forecast slots factually per-fact); "sum the partial months" contract override for `BUCKET_INCOMPLETE`; the §4 Phase-2 deletions/consolidations; live one-tap answer flow exercised end-to-end in the UI.

---

## 12. Phase 2 delivery record (2026-08-05, same day)

**The flag is gone and so is the legacy binder.** `TEMPO_FILL_PLAN` removed; `bind()`/`binding.py` deleted (shared fact/evidence helpers moved into `execute.py`); every populate runs plan → contract overlay → verify → repair → execute. The historical binder regression suite (scenario variants, double-count guard, sign conventions, budget columns, FTE protection) was ported to exercise the real verify/execute path via `tests/planpath.py`.

**Deleted/demoted per §4:** `is_count_like` + count lexicons (planner declares counts); `resolve_scale`/`series_scale` + the modal `fallback_scale` guess (execute's declared-units + evidence model replaced them); the rollup cascade, scenario-gating judgment, sign silent-override and confidence-kill died with `bind()`. **Consolidated:** currency lexicons 4 → 2 (`units.CCY_TOKENS` canonical; numfmt keeps its format-symbol map); date parsing 3 → 2 (`derive` now delegates to `periods.parse_any_date` — DERIVATION_VERSION 11; `temporal_analyzer`'s status classifier remains its own pass, revisit in Phase 3). **Visibility:** source sheets skipped by the densest-N selection surface in `routing`; `unused_source_series` files one batched "did I miss something?" item; rescue emits full fill-plan fields.

**New evidence model (from a live gate finding):** with no row magnitudes, the template's *anchored rows* imply its DISPLAY BASE (`target_base = source_base / verified_scale` — invariant across source sheets with different bases). A plan whose declared target unit contradicts it becomes a `SCALE_CONFLICT` question; corroboration fills clean. First version compared raw factors and broke Meridian's `'000→m` ARR against raw→m flash rows — the display-base invariant fixed it. This replaces the legacy modal-guess with ask-don't-guess.

**Gate:** Meridian 113 fills / 9-of-9 verified correct / 0 wrong / 0 bad fills through the single path (incl. a v11 re-derivation). PE-flash: 72 fills; the only drift vs legacy is the correct refusal of the wrong revenue-per-employee row; unverifiable P&L scales fill flagged+reconcile-questioned (no ground truth exists — expected cells to be hand-verified). 330 offline tests.

**Phase 3 next:** derive category-cascade lexicons → LLM input-identification grounded on facts; color palette and placeholder/adjustment lexicons demoted to priors; one "is 0 empty" rule; gate on the owner's 1/2 ground-truth labels.

---

## 13. Phase 3 + 4 delivery record (2026-08-05, same day)

**Phase 3 — lexicon categories became overridable PRIORS.** `DataPoint.category_source` records WHO decided a category (`lexicon:<kind>` / `llm` / `user` / None=factual); `merge._llm_may_write` lets LLM enrichment override lexicon-sourced categories only (facts and user values stay untouchable; user beats all); enrichment may now answer `category=data` to REVERSE a wrong lexicon call (persisted only when it reverses one), and its items show the lexicon's opinion. A connector formula (a fact) now beats a junk-looking label (a prior) — the mismatch is flagged and stamped overridable, instead of silently deleting connector data. One "is 0 empty" rule everywhere (0 is a value). Shared prior lexicons live in `app/priors.py` — with a STRICT placeholder tier for the data model (bare "Other"/"New" are real P&L lines) and a loose tier for region-slot corroboration; adjustment banks likewise split broad (ranking-only) vs strict (region conversion). The understanding grid gained a `[fill]` marker (non-input, non-near-white solid fills visible to the model; sheet cache v8). DERIVATION_VERSION=13. Requires migration **0011** (`category_source` column); until applied, a loud fallback strips the field rather than crashing — proven live at the gate.

**Phase 4 — scoped contract.** `contract.source_fingerprint` (sheet-name-set hash = source FAMILY identity, stable across monthly editions); `decision_spec` carries `scope` (+fingerprint); unit answers file as `source_format`-scoped with a per-family item_key (so a second source family gets its own question instead of a deadlock — an adversarial-review catch); `load_decisions` filters by the incoming source's fingerprint; pre-scope answered items keep applying (backwards compatible). Global promotion is deliberately NOT a code path — it goes through the rule ledger with multi-template evidence.

**Multi-agent review (owner-directed):** an adversarial correctness agent (7 findings: 1 critical — the missing DB column that would have crashed-and-wiped on the next real derive; the source-family question deadlock; 3 union-regression cases incl. bare-"Other"→config; the near-white `[fill]` flood) and a simplify/efficiency agent (15 items: stale binder narrative, triplicated sheet-grain computation → `periods.sheet_grains`, per-cell→per-row evidence voting, `pick_column` wrappers moved to tests, PlanIssue.scenario instead of prose-parsing, `resolve_unit` memoized, MODEL alias retired, etc.) — all 22 verified findings applied by a fix agent. `docs/RULE_LEDGER.md` created: 57 surviving rules, 30 constraints / 27 priors, deleted-rules history included.

**Gate:** 340 offline tests; live Meridian 113 fills / 9-of-9 verified correct / 0 wrong / 0 bad fills through a v13 re-derivation, with the pre-migration fallback exercised live. PE-flash stable at 72 fills.

**Still open (needs the owner):** apply migration 0011; onboard the "Templates for testing/" ground-truth files and run the 1/2-label accuracy eval (the full Phase-3 gate — LLM input-identification quality can only be measured against those labels); exercise the one-tap answer flow in the UI end-to-end.

---

## 14. Write-Semantics Authority Fix (2026-08-06)

The PROD ground-truth eval exposed the onboarding-side authority violation: the understanding CORRECTLY claimed formula cells as inputs (type-over fronts of a connector template, incl. Dec-22, and the renameable company-account label column) — derive's absolute `formula → computed` rule discarded the claims, and the July `passthrough.py` workaround was silently resolving the WRONG cells (0-based refs against 1-based maps: everything one row up, one column left — the "Dec-22 cliff").

**Shipped (plan: `~/.claude/plans/replicated-leaping-curry.md`):**
- `app/datamodel/topology.py` (new): 1-based ref extraction; `is_multi_input` — THE structural constraint (a formula reading a range or ≥2 cells is a computation, never converted; catches ratios/AVERAGE that `_is_aggregation` alone misses — design-validator catch); CX_PUSH inventory (`push_targets`, call-shape regex) — entry cells by the workbook's own declaration.
- **Inversion in `_emit`**: claimed formula cell (or push-read cell) + not multi-input → `sourced`, `write_mode="type_over"`, `category_source="llm:input_field"|"topology:push"`. Blank push-read cells beat the sheet-role prior. Hidden rows/cols (now captured in the parse) beat writability: `staging`, `geometry:hidden`. All stamped + review-flagged + user-correctable.
- **DELETED**: `passthrough.py`, its derive pass + backing checks, its tests (3 semantic invariants ported to `tests/test_type_over.py` incl. the ratio tripwire). eval/label_ingest aligned to the shared constraint.
- Parser fact repairs: shape text extracted for real (no more `<FontSettingCollection>` reprs; group shapes recursed) — the user guide reaches the model; hidden_rows/hidden_cols in the snapshot; `[hid]` grid marker + hidden-cols note; PUSH-ENTRY hint line (the push columns sit beyond the 80-col grid cap). Sheet cache v9.
- Plumbing: `DataPoint.write_mode` + migration **0012** (apply with 0011); `replace_data_points` strip fallback generalized to all optional columns; merge: `write_mode` is user-only; reset guard extended (unfilled type-over cells keep their default formula). DERIVATION_VERSION=14.

**Owner actions:** apply `supabase/migrations/0012_write_mode.sql` (with 0011); re-run Understand on the three test templates when convenient (cache v9 — picks up guide text, [hid], PUSH-ENTRY hints; the inversion itself works on the STORED understanding immediately). Remaining stages tracked: usage brief injection (Stage 3), renameable-label writes + configurable-list onboarding questions + flash Budget-rows defect (Stage 4), eval consolidation (Stage 5).
