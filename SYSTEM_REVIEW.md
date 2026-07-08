# Project Tempo — End-to-End System Review (for external stress-test)

**Purpose of this doc:** plain-English explanation of how the system works today, where it breaks, why it costs so much, and the proposed fixes. Written to be handed to another LLM with full repo access to pull apart and challenge.

**What the product does:** A PE firm uploads an Excel reporting template (flash report, etc.). The system "understands" that template once. Later, a portfolio company drops a data file (their management accounts) onto the template, and the system should copy the right numbers from the data file into the right cells of the template — correctly scaled, correct sign, correct period, correct currency — and produce a filled workbook plus an audit trail.

**Stack:** Python / FastAPI backend, Aspose.Cells for Excel parsing/rendering, Anthropic Claude (`claude-opus-4-8`) for all LLM steps, Supabase (Postgres + Storage) for persistence, Next.js frontend.

---

## 1. How it works, end to end

There are two phases: **Onboarding a template** (done once per template) and **Populating** (done every time a data file is dropped).

### Phase A — Onboarding a template (runs once)

| Stage | What it does | LLM? | Vision/images? | Cost shape |
|---|---|---|---|---|
| **1. Parse** | Aspose reads the workbook into structured data (every cell: value, formula, precedents, styles). Saved as a compressed "snapshot" JSON. | No | No | Cheap |
| **2. Structure (L2)** | Pure Python. Detects metric rows, period columns, sections, input regions, which cells are inputs. | No | No | Cheap |
| **3. Understanding (L3)** | For up to **16 sheets**, renders each sheet to **1–4 PNG images** and sends image + full text grid + a very large system prompt to **Opus** to describe the sheet (metrics, periods, inputs, rules). Then **1 more Opus call** synthesises a workbook-level summary. | **Yes** | **Yes (per sheet)** | **Expensive** |
| **4a. Data model (derive)** | Pure Python. Combines L2 + L3 + snapshot into one "fact" per input cell (metric, period, scenario, unit, category). This is the "Template Contract". | No | No | Cheap |
| **4b. Enrich (optional)** | 1 Opus call to assign canonical metric names to the distinct metrics. Text-only. | Yes | No | Moderate |

Output of Phase A: a stored, reviewed **data model** ("what each input cell in this template means"). This is cached and reused.

### Phase B — Populating from a dropped data file (runs every time)

| Step | What it does | LLM? | Vision? |
|---|---|---|---|
| **Parse source** | Aspose parses the dropped data file into a snapshot (in memory, never stored). | No | No |
| **Route** | 1 Opus call: which source sheets likely hold which template sheet's data. | Yes | No |
| **Match** | The core step. The template's input list is split into batches of 100 cells. For each batch, the source sheets are **rendered to images** and sent to **Opus**, which must return, for each template cell: the **source cell address it can "see" in the picture**, a **`unit_scale`**, and a **`sign_flip`**. Runs ~6 calls in parallel, up to 3 source-sheet images per call. | **Yes** | **Yes (source rendered to images every run)** |
| **Apply** | Pure Python. Reads the real source value at each address from the snapshot, applies scale/sign, writes a "filled" record. | No | No |
| **Render** | Opens the template workbook, clears stale numbers in input cells, writes the matched values, uploads filled workbook + JSON audit. | No | No |

The important architectural fact: **`apply` already reads exact values structurally from the parsed snapshot** (every value and address is available as clean data). But the step before it (`match`) ignores that and instead asks a vision model to read the same numbers off a *picture* and guess their addresses.

---

## 2. The gaps (why the output is wrong)

These are taken from a real run (source: "Dream Games – Financial Report 2026.03.xlsx").

1. **Matching is done by looking at pictures, not data.** The matcher renders the source spreadsheet to images and asks Opus to (a) read numbers off the grid, (b) guess the A1 cell address it sees, (c) guess the scale. This is OCR-plus-guessing over data the system already holds precisely. It is the root cause of everything below.

2. **The scale is guessed per cell, so a single row is internally inconsistent.** Example — Trade Payables, one row, consecutive months: Apr-23 came out as **53,607**, May-23 as **51**. Same metric, same row. The model used `unit_scale = 0.001` for the early months then flipped to `1e-06` for later months, because it was judging scale from how each number *looked*. Result: 1000× cliffs mid-row.

3. **Hallucinated / wrong source rows.** "Trade receivables" was filled with `0.0`, `0.00001`, `-1.01`, `-82`, `147` — noise and negatives. It matched a label that looked similar in the image rather than the correct data series.

4. **Currency is ignored.** Source is in USD, template is in EUR. The audit notes literally repeat *"FX not applied."* So even correct mappings produce wrong numbers.

5. **No confidence floor.** Links with confidence as low as **0.25–0.45** are written straight into the workbook. There is no threshold and no "leave blank / flag for review" path.

6. **Low effective coverage with high spend.** In the latest run the summary was `filled: 2, unmatched: 1054` — i.e. it spent heavily and filled almost nothing, because the test source was itself a formula-driven workbook with little cached numeric data. The pipeline doesn't distinguish "no data available" from "couldn't match," so it burns full cost either way.

---

## 3. Why the cost is so high (~$600 in 4 days)

Four compounding factors, all in the LLM layer:

1. **Everything runs on Opus 4.8 — the most expensive model — with "adaptive" thinking (high effort) and large output budgets (24k–32k tokens).** Nothing is routed to a cheaper model, even steps that are simple text mapping.

2. **Vision tokens dominate.** Both Understanding (onboarding) and Match (every populate) send rendered spreadsheet images (150 DPI PNGs, up to 4 tiles per sheet) to Opus. Vision input is far more expensive than text, and the same grid data exists as cheap text/structured form.

3. **Match re-runs on every single populate, and it's the vision-heavy step.** Onboarding's vision cost is one-time per template, but matching pays the image+Opus cost on every drop, batched across ~hundreds of cells × multiple sheets × parallel calls. Iterating on one template a few times a day is what ran the bill up.

4. **Large fixed overhead per call.** A very large system prompt plus the full text grid is sent on top of the images for each per-sheet / per-batch call, and failed/truncated calls retry — so each run is many large, expensive requests.

In short: the most expensive model + vision + a per-run vision step + big prompts + retries.

---

## 4. Proposed solution

**Core principle: the LLM should map meaning, not read numbers.** It should never see pixels and never read values. The exact values and addresses are already in the parsed snapshot — use them.

### 4.1 Rebuild the matcher as text-only, row-level semantic mapping
- The LLM's only job: map each **template metric** (with its label, section, and declared unit) to a **source data series** (a labelled row/column in the source), using the **text labels and structure** — not images, not values. Map **once per metric (~hundreds)**, not once per cell per period (~thousands).
- Deterministic code then does everything numeric:
  - **Expand** each row-level mapping across all period columns automatically.
  - **Read** exact values from the source snapshot by address.
  - **Scale once per series**, derived from the source's own declared units (detected once from headers/format — e.g. "$m" vs raw), so a 1000× cliff inside a row becomes impossible.
  - **Handle currency explicitly**: detect source vs template currency; either convert with a supplied rate or refuse and flag. Never silently mix.
  - **Apply a confidence floor**: below a threshold, leave blank and flag for review rather than writing a guess.

Expected effect: roughly two orders of magnitude fewer/cheaper tokens (text vs vision, per-metric vs per-cell), and the structural elimination of scale/row/currency errors because no number is ever eyeballed.

### 4.2 Route by difficulty, not always Opus
- Text-only semantic mapping is a good fit for **Sonnet or Haiku**. Reserve Opus for genuinely hard reasoning, if anywhere. Lower `max_tokens` and reconsider always-on high-effort thinking for simple steps.

### 4.3 Make "no data" cheap and explicit
- Before spending on matching, check whether the source actually contains numeric data for a metric. If a series is absent, mark it unmatched deterministically instead of paying the model to fail.

### 4.4 Keep what already works
- Parse, Structure (L2), the deterministic data model (L4a), and the `apply`/`clear`/`render` layer are sound and stay. The change is concentrated in the `match` step (and a model-routing change). Onboarding's Understanding step is also vision-heavy but runs once per template; it's a secondary optimisation, not the recurring bleak.

---

## 5. Questions to stress-test (for the external reviewer)

1. Is row-level semantic mapping + deterministic period/value/scale expansion actually sufficient, or are there real templates where matching genuinely needs to "see" layout (e.g. forms with no usable labels)? If so, how should those be detected and handled as the exception rather than the default?
2. Is detecting "one scale per source series" from headers/number-format robust across messy real-world management accounts, or does it need a fallback (e.g. sanity-check against magnitude continuity within a row)?
3. What's the right currency policy — require an explicit FX rate at populate time, or attempt detection? What's safest for a trust-sensitive PE context?
4. Where exactly should the confidence threshold sit, and what's the review UX for flagged/blank cells?
5. Is there a case for dropping the LLM from matching entirely for well-structured sources (deterministic label matching + embeddings) and only invoking a model for ambiguous leftovers?
6. Onboarding still uses per-sheet vision on Opus. Is that justified, or can structure+text carry most templates with vision reserved for visually-encoded sheets only?
