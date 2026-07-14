# Project Tempo — System Reliability Audit

_Grounded audit, 2026-07-14. Every claim tied to a file, prompt, schema, or data object. Produced from four read-only codebase investigations (onboarding LLM layer, data model/contract, population+mapping path, test harness) plus session evidence. No code was changed._

---

## 1. Executive diagnosis

The system **is** built on the right skeleton — Aspose extracts a rich fact snapshot, deterministic code owns value placement with full traceability, and there is a genuinely sophisticated free-judgment LLM pass (per-sheet understanding, Opus + image + grid). But reliability leaks from **four structural places**, in order of impact:

1. **"Is this a required input?" is decided by rules, not grounded LLM judgment.** There are *two* input detectors — a deterministic six-signal detector (`structure/input_detector.py`) and the LLM's `input_fields` — and **they are never unioned**; the LLM is asked only about *form-fill* cells and is handed a *subset* of the deterministic signals as hints. The *fillability category* (`data/sourced/computed/config/staging`) is then decided by a **deterministic cascade** (`derive._classify_category`) that overrides meaning with rules like `formula → computed`. Every failure hit this session (config cells, connector cells, transposed grids) lived here.

2. **The LLM that judges is starved of decisive facts** — no number formats, no conditional formatting, no dependents, no reporting date, definitions truncated to 90 chars, rows past 350 dropped.

3. **The "contract" is not durable** — it's a re-derivable per-cell fact cache plus a corrections patch-set; `entity` is hardcoded null, no "why", confidence mostly a hardcoded `0.5`, audit is a JSON blob with no key back to the fact.

4. **There is no way to know if any of this is getting better** — zero golden templates, zero input precision/recall, every LLM step monkeypatched in tests.

**The disease in one line:** meaning decisions are encoded as deterministic rules, the LLM that should make them is under-fed, and there's no measurement to catch either.

---

## 2. Pipeline map (template upload → output)

| Stage | File / function | Input → Output | Decider | Information lost / compressed |
|---|---|---|---|---|
| Parse | `raw_extraction/workbook_parser` → `snapshot.workbook_to_snapshot` | xlsx → snapshot dict (cells: value, cached_value, formula, style, **precedents**) | Deterministic | **Dependents not stored** (only precedents); conditional formats, chart captions, hyperlinks, file metadata kept in snapshot but never propagated |
| Sheet routing | `understanding/workbook.route_sheets` (+ `_triage_ambiguous`, Sonnet) | snapshot → deep/light/skip set | Both (LLM only on ambiguous) | Triage LLM sees only 12 rows of addr=value; can only save cost, never lose a sheet |
| Per-sheet understanding | `understanding/per_sheet.understand_sheet` (Opus, image+grid) | grid+image → `SheetUnderstanding` | **LLM (free judge)** | Rows>350/cols>80 dropped; bodies>90 chars clipped; **no number format / conditional format / dependents / reporting date** |
| Cross-sheet synthesis | `understanding/workbook.synthesize` (Opus) | compact per-sheet JSON + dependency edges → `WorkbookUnderstanding` | LLM constrained by graph | Sees `metric_rows[:40]`/sheet, input fields reduced to a **count**, no grid/formula/style |
| Dimensions enrichment | `datamodel/dimensions_llm._classify_batch` (Opus) | `{sheet,role,label,unit}` → canonical/basis/category | **LLM (pure classify, 4 fields)** | Sees nothing but 4 fields — no value, formula, section, neighbors |
| Cell classification / input detection | `datamodel/derive._emit` + `_classify_category` | facts + hints → `DataPoint[]` with `category` | **Deterministic cascade** | The LLM's rich `input_fields` and six-signal detector are *not* the authority here — rules are |
| Formula/precedent analysis | `population/aggregation.metric_totals`; `understanding.verify` | snapshot precedents → total-leaf map / graph_supported | Deterministic | Only aggregation totals + flow verification; no dependents graph exposed to LLM |
| Metric/period inference | `derive.period_for`, `_cx_identity` | grid + CX args → period/metric | Deterministic | Column-oriented periods only (row-oriented needed CX-arg rescue); relative periods get **no absolute date** |
| Contract creation | `datamodel/persist.derive_and_persist` + `merge.apply_corrections` | facts → `template_data_points` + corrections | Deterministic | **Re-derived each version**; entity null; no reason/evidence; confidence mostly 0.5 |
| Source parse | `population/run` → `source_understanding.understand_source` (Sonnet) | source xlsx → structure (`series`, `periods`) | Both | Top-8 sheets only; **no values reported**, ≤8 sample hints; formulas→cached values |
| Source→template mapping | `population/mapping.map_metrics` (Sonnet, batch 80) | deduped metric labels + ≤5 source samples → `MetricMap[]` | **LLM (meaning)** | **No cells, no periods, no sheet, defs clipped to 90 chars, ≤5 source samples** |
| Write proposals | `population/binding.bind` + `apply.apply_links` | maps + snapshot → `CellLink[]`/`FilledCell[]` | **Deterministic ("no LLM in the loop")** | Correct by design — placement, scale, sign, period, double-count all deterministic |
| Validation / audit | `population/run` + `template_checks` + `sb.upload_audit` | filled → recalc checks + audit JSON blob | Deterministic | Audit is a **storage blob, not a table**; `FilledCell` carries **no `fact_key`** |

---

## 3. Rule-vs-LLM decision table (meaning made by rules)

| Decision | Current rule | Location | Embedded assumption | Fails when | Should be |
|---|---|---|---|---|---|
| Is a cell a computed output? | `has formula → computed (not input)` | `derive._classify_category` | A formula is never a business input | Connector `CX_GET` cells hold the company's financials | **LLM judgment** (does this fetch/compute an input value?) |
| Is a cell an input? | six-signal detector (unlocked/fill/graph/validation/numeric-in-period) | `structure/input_detector.py:100-127` | Inputs look like blank/unlocked form cells | Connector cells are locked formulas; detector skips them | **LLM judgment** + signals as evidence |
| Is a cell config/selector? | label lexicon (`\bmode\b`, `selection`, `override`, `KPI Label N`…) | `derive._CONTROL_RES`/`_PLACEHOLDER_RES` | Controls are namable by regex | A selector labelled "Scenario"; a real metric containing "mode" | LLM judgment (rule as fast-path prior) |
| Periods are in columns | period detector reads column headers; **skips `orientation=="row"`** | `derive` period loop | Metrics in rows, periods in columns | Chronograph transposed grid (periods down col F) | LLM reads layout; deterministic resolves addresses |
| Metric identity | `label_as_written` → row label → column-A grab → `"row N"` | `derive._emit:747-754` | The metric label is in the row's left cells | Multi-block rows / transposed grids mis-attribute | Rescued only by CX-arg parse; else needs LLM layout read |
| Basis/canonical/category | LLM over **4 fields** (sheet/role/label/unit) | `dimensions_llm.SYSTEM:62-73` | Label+unit are enough to classify | "Adjusted" vs "Reported"; stocks vs flows need value/formula/section | LLM but **grounded** (currently starved) |
| Confidence | `l3m.confidence or 0.5` | `derive._emit:818` | Non-L3 cells are all equally (0.5) certain | Can't prioritize which inputs to review | Real per-decision confidence from the judging LLM |
| Entity | `entity = None` | `derive._emit:812`, `:923` | Single-entity templates | Multi-entity / multi-fund templates (Chronograph `[Inv]`) | LLM/structure must populate it |

**Verdict:** the deterministic value-placement rules (scale, sign, period-bucket, double-count in `binding.py`/`units.py`) **should stay deterministic** — they're the trust guarantee. Every row above about *what a cell means / whether it's an input* **should move to grounded LLM judgment**.

---

## 4. LLM context audit (what each call actually sees)

**7 LLM calls total.** The one that matters most — per-sheet understanding — is a genuine free judge; the rest degrade sharply.

| Call | Model | Judge or classify? | Sees | Starved of |
|---|---|---|---|---|
| Triage | Sonnet | **Classify** pre-flagged sheets | 12 rows addr=value, routing stats | formulas, styles, image |
| **Per-sheet deep** | **Opus** | **Free judge** | grid (labels/values/formulas/bold/indent/`[in]`/`[unlocked]`/`[mrg]`/`[grp]`), image, comments, textboxes, validations, named ranges | **number formats, conditional formatting, non-whitelist colors, dependents, reporting date, workbook metadata**; rows>350/cols>80 gone; bodies>90 chars clipped |
| Per-sheet light | Sonnet | Free judge (no image) | grid only | + the image |
| Synthesis | Opus | **Constrained** (graph owns truth) | compact per-sheet JSON, dependency edges, named ranges | grid, image, formulas, styles; `metric_rows[:40]`, inputs → a **count only** |
| Dimensions enrich | Opus | **Pure classify** | `{sheet, role, label, unit}` — 4 fields | value, formula, section, neighbors, everything else |
| Source understanding | Sonnet | Structure only | grid + ≤8 sample values, image | full values, formulas, number formats |
| **Mapping** | Sonnet | **Meaning match** | deduped metric labels, def≤90 chars, qualifies≤110, ≤5 source samples, 6k-char context | **target cells, periods, sheet, scenario shape, full source values, hierarchy** |

**Input-field instruction, verbatim** (`prompts.py:157`): _"input_fields — the cells the portfolio company actually FILLS IN. Combine the image's input-styled cells, 'please provide' prompts, validations, and the deterministic hints."_ → framed as **form-fill cells**, which is precisely why connector-fed financial inputs weren't asked about.

**Mapping prompt is genuinely sophisticated** (5-way status: direct/aggregate/reconcile/needs_decision/unavailable with double-count discipline, `mapping.py:26-93`) — but it decides identity from labels + 5 magnitude samples + clipped definitions. Its real failure mode is **mis-identifying financially adjacent lines** (Adjusted vs Reported EBITDA) when the distinguishing text exceeds the 90-char cap.

---

## 5. Data-model / contract audit

**Persisted per-cell `DataPoint`** (`schema.py:43-83`, table `template_data_points`): fact_key, sheet/cell/row/col, metric_label, canonical_metric, period_index, parsed_date, period_type, scenario, basis, **entity**, unit, currency, value_role, sign_convention, qualification_criteria, definition, expected_source, needs_value, **category**, scenario_source, basis_source, confidence, applied_correction_ids.

- **Distinctions that work:** input vs config vs computed vs sourced vs staging vs exclude (the category cascade); totals protected by `value_role`.
- **Missing:** `entity` is **hardcoded null** (`derive.py:812,923`) — multi-entity templates cannot express per-entity inputs. No `assumption` field on the contract (only appears at populate time). No dedicated "user override" flag.
- **Opportunistic completeness:** `unit`, `sign_convention`, `definition`, `expected_source`, non-0.5 `confidence` are present **only when an L3 metric row matched that row** (`l3m = l3_by_row.get(row)`); connector/detector cells get null + 0.5.
- **No "why":** no evidence/reason field for *why a cell is an input*; provenance is tracked for **only 2 dimensions** (scenario_source, basis_source).
- **Explainability:** within a run, traceable (`FilledCell`/`CellLink` carry source cell, raw value, scale, sign, note) — **but it's a storage JSON blob, not a table, and carries no `fact_key`**, so "explain cell AD20" is a fuzzy (sheet, cell, label) join.
- **Not durable:** facts **auto-re-derive** whenever `DERIVATION_VERSION` bumps; `template_contract` stores only status/notes; the reviewable contract surface is **rebuilt live** each call (`get_contract_fields`). The only version-spanning artifact is `template_corrections`.

**Verdict:** this is **a re-derivable per-cell fact cache + a correction patch-set, not a frozen, entity-complete, evidence-backed contract of required inputs.**

---

## 6. Failure autopsy (grounded)

| # | Expected | Actual | Code path | Root cause | Class | Auto-detect |
|---|---|---|---|---|---|---|
| 1 | "POC Mode"/"Scenario Selection" excluded | Treated as `data` inputs | `derive` blank/literal→data | **Bad rule** (no config concept) | bad rules | golden template w/ known config set → assert excluded |
| 2 | Connector cells = replaceable inputs | Invisible (9/857) | `input_fields`=form cells; `formula→computed`; six-signal skips locked formulas | **Bad rule + weak prompt** | rules + prompt | count `sourced` connector cells vs snapshot connector count |
| 3 | Transposed grid metrics/periods identified | `"row N"`, no dates | `period_for` skips `orientation=="row"` | **Bad rule (layout assumption)** | rules | assert every fact has a period on a known-transposed fixture |
| 4 | Per-entity inputs (fund `[Inv]`) | All entity=null | `entity=None` hardcoded | **Missing data-model field** | data model | assert entities≠[] on multi-entity template |
| 5 | Grain×scenario matrix cells mappable | No `parsed_date`, unmappable | L3 periods are *relative* labels; no reporting-date resolution | **Missing extraction (reporting date)** | missing extraction | % facts with parsed_date on summary sheet |
| 6 | Adjusted vs Reported EBITDA distinguished | Mis-map risk | `def` clipped to 90 chars (`mapping.py:128`) | **Weak schema/truncation** | insufficient LLM context | mapping-accuracy test on a definition-sensitive pair |
| 7 | sign/unit/expected_source populated | Null off-L3 rows | `derive` pulls only from `l3m` | **Missing data-model coverage** | data model | % inputs with null sign/unit |
| 8 | Confidence reflects certainty | Hardcoded 0.5 | `derive._emit:818` | **Missing signal** | data model | distribution of confidence==0.5 |
| 9 | Workbook-level reasoning sees inputs | Sees only a *count* + 40 metrics | `_compact` caps (`workbook.py:274`) | **Insufficient LLM context** | LLM context | n/a (structural) |
| 10 | Large sheets fully read | Rows>350/cols>80 dropped | grid window caps | **Silent truncation** | missing extraction | flag when a sheet exceeds the window |
| 11 | £/$/% and 000s/m understood | Number format never sent | `sheet_view` omits `number_format` | **Insufficient LLM context** | LLM context | n/a |
| 12 | RAG covenant breach seen | Conditional formatting invisible | stripped from render + grid | **Missing extraction to LLM** | LLM context | n/a |
| 13 | Period status (hist/current) grounded | Hardcoded "unknown" | reporting date = `"unknown"` (`run.py:42`) | **Missing extraction** | missing extraction | n/a |
| 14 | Know if a change improved detection | Can't measure | no golden set / no P-R | **No test harness** | missing validation | *this is the meta-detector* |
| 15 | "Why was cell X written?" queryable | Blob, no fact_key | audit JSON (`run.py:705`) | **Missing lineage field** | data model | assert FilledCell carries fact_key |

---

## 7. Testing / evaluation gaps

**Blunt verdict: we do not have a way to know whether the system improved.**

- **No golden templates.** Zero committed `.xlsx`/golden-JSON fixtures; every test workbook is 2–4 Aspose cells built in-test or a hand-built dict. A real 16-sheet snapshot (`parser/snap.gz`, 72k cells) sits **referenced by nothing**.
- **No expected-input ground truth**, **no input precision/recall/F1**, **no mapping-accuracy** measurement. `test_matcher.py` explicitly **stubs the LLM mapping** and proves only the deterministic arithmetic.
- **No end-to-end run on a real file** (onboarding LLM path never runs un-monkeypatched).
- **What IS strong** (288 tests): deterministic bind/scale/sign/scenario (`test_matcher.py` 47), double-count guard, config/connector/CX classification, regions, corrections, verify-after-fill. Coverage gaps: **YTD/LTM grains (NONE)**, merged-cell semantics (marker only), named-range resolution (partial).

Single biggest gap: **a golden-template corpus with expected-inputs and expected-mappings, plus precision/recall scoring.**

---

## 8. Prioritised rebuild plan

| # | Gap | Evidence | Why unreliable | Correct design | MVP fix | Robust fix | Effort |
|---|---|---|---|---|---|---|---|
| 1 | **No eval harness** | §7 | Can't tell if any change helps/hurts | Golden corpus + P/R + mapping accuracy, run in CI | Seed 3–5 real templates (start with `snap.gz`) + hand-labelled expected inputs; a scorer for input P/R | Full corpus across archetypes + mapping-accuracy + layout regression suite | **3-day** MVP, 1-week robust |
| 2 | **Input-ness decided by split rules** | §3, failures 1–3 | Every new template shape breaks a rule | One LLM "input judgment" pass, grounded on the full fact pack, six-signal as *evidence* | LLM call: "for each candidate region, is this a required input, what kind, why?" over grid+facts, verified against snapshot | Region-level judgment feeding a frozen contract w/ reason+confidence+evidence | **1-week** |
| 3 | **LLM starvation** | §4 | Judge can't see decisive facts | Put number formats, conditional formatting, dependents, reporting date, un-clipped definitions into the fact pack | Add number_format + CF summary + reporting-date extraction to grid/annotations; raise def cap in mapping | Full "fact pack" builder shared by understanding + mapping | **3-day** |
| 4 | **Contract not durable/complete** | §5 | Re-derives, entity-null, no why/confidence | Freeze an approved contract: explicit required-input spec w/ entity, reason, confidence, evidence; re-derive proposes diffs | Populate `entity`; store per-input reason+evidence; add `fact_key` to FilledCell | Versioned frozen contract + queryable populate-run/lineage tables | **1-week** |
| 5 | **Reporting direction unbuilt** | failure 5 | Grain×scenario summary sheets can't fill | Reporting-date anchor + grain/scenario dims + YTD/LTM rollup engine | Anchor reporting date + fill Monthly | General temporal-rollup engine | **multi-week** |

---

## 9. Direct answer: is it implementing the intended design?

**Partially.**

- Aspose extracts grounded facts → **YES** (rich snapshot), though CF/dependents/metadata never reach the LLM.
- LLM interprets business intention → **YES for meaning** (the per-sheet Opus pass is a real free judge), **NO for required inputs** — input-ness is decided by a deterministic cascade + a form-field-framed prompt + a starved 4-field enrichment call. **This is the fake/weak part.**
- Durable template contract → **NO** — it's a re-derivable fact cache + corrections; entity-incomplete, no reason/evidence, transient.
- Future data mapped using that contract → **PARTIALLY** — mapping uses a compressed, re-derived subset (deduped labels, 90-char defs, 5 samples), not a locked spec the source is validated against.
- Deterministic writes with traceability → **YES** (strong; minor gaps: blob not queryable, no fact_key link).

**What is breaking and why:** the two ends are solid (fact extraction; deterministic, traceable writes). The **middle — "identify every required input and lock it into a durable contract" — is where it breaks**, because that judgment is done by rules and a starved/mis-framed LLM instead of a grounded judgment layer, and nothing measures the result.

---

## 10. What to build next

**Do these two first, in this order — highest reliability-per-effort, and everything else depends on them:**

**(A) The eval harness (3 days).** You cannot fix reliability you can't measure, and we currently can't. Take `snap.gz` + 2–4 more real templates, hand-label the expected required-inputs per template, and write a scorer for input precision/recall. This becomes the regression gate for everything below.

**(B) The grounded "input judgment" LLM layer (1 week)** — the replacement for the brittle part:

- **New layer:** after facts + per-sheet understanding, one LLM pass judges input-ness per region.
- **Input it receives (the fact pack):** for each region — labels, values, formula strings, **number formats**, **conditional-format verdicts**, precedents **and dependents**, styles, neighbors, the **resolved reporting date**, section/purpose context, and the deterministic six-signal verdicts *as evidence, not truth*.
- **Output schema:** per input — `{cell_or_range, is_input: bool, input_kind: typed|connector_fed|selector|computed_output|override, metric, period, scenario, entity, unit, sign_convention, expected_source, reason: str, confidence: float, evidence_cells: []}`.
- **Deterministic verification:** every `evidence_cell`/address validated against the snapshot (LLM may not invent addresses); connector/formula facts cross-checked; category consistency enforced.
- **Human review:** the existing contract surface shows each input + reason + confidence; one-tap confirm/correct; corrections persist (already built).
- **Storage:** **freeze** the approved result as the durable contract (with entity, reason, confidence, evidence) — not a re-derivation.
- **Future runs:** the frozen contract is the demand spec the source is mapped/validated against; re-derivation only proposes *changes* for re-approval.
- **Stays deterministic (never trust the LLM with):** numeric values, cell addresses (must be grounded+verified), unit scale, sign resolution, the final write, the formula/precedent graph, the double-count guard.

Then **(C)** un-starve the mapping call and **(D)** build the reporting-direction rollup — but only after (A) exists to prove they help.
