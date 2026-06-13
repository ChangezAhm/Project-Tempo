# Layer 3 — LLM Template Understanding (Design)

> **Status:** Design for sign-off. Builds on Layer 2 (deterministic snapshot +
> structure tables) and Option B (full snapshot in Storage).

## Context

The deterministic extractor breaks on real PE PortCo templates — rigid "labels
in cols A/B/C + indent hierarchy + keyword metrics" produced **0 metric rows**
on the sheets that matter (`PortCo_Input`, `Multiple`, `CapTable_depr`) because
their layouts don't obey those rules, and hierarchy was unreliable (3 of 1,922
rows linked). The IPV file is a valuation/IPV workbook (multiples + DCF + cap
table → EV→equity bridge) — the kind of layout a human analyst reads on sight
and code can't. **Layer 3 makes the LLM the primary engine for structural and
semantic understanding, grounded on the deterministic facts so it can't
hallucinate.**

## Principle: facts deterministic, judgment LLM, everything cited

- **Deterministic (ground truth, never overridden):** cell values/formulas,
  the dependency graph + impact, validations, named ranges, text boxes,
  locking. Layer 1/2 already produce these.
- **LLM (judgment):** where the labels are, what each metric is, hierarchy,
  sections, the input surface, financial meaning (Reported vs Covenant vs
  Valuation EBITDA), author rules, data flow.
- **Every LLM claim cites cells/formulas/text-boxes and gets a confidence
  score; citations are validated against the snapshot; low-confidence items are
  flagged for human review.** This is the consultant-trust moat.

## The unlock: show the LLM the sheet (hybrid grid + image)

Per meaningful sheet we build a **sheet view** so the model reads the real
layout instead of our broken guess:

1. **Text grid** (from the snapshot, no re-parse): a compact spatial rendering
   of the used range — rows as rows, cols as cols, each non-empty cell showing
   value or `=formula` + light markers (bold, input-fill, locked/unlocked,
   indent, number format), with text boxes/validations anchored to their cells,
   and **cell addresses preserved** so the model cites `H12` not "row 12".
2. **Sheet image** (Aspose renders the sheet/used-range to PNG): the model
   *sees* banners, colored input cells, section blocks, indentation — how the
   template is designed to be read.

Hybrid because the image gives layout comprehension and the grid gives exact,
citable data. Big sheets are tiled; data-dump sheets are skipped (routing).

## Pipeline (grounded, multi-agent)

```
route (cheap)  →  per-sheet understand (parallel)  →  synthesize  →  verify  →  compile
```

1. **Route** — classify each sheet's role (input / calc / lookup / data-dump /
   cover / instructions) from name, tab colour, cell density, formula ratio,
   text-box presence. Skip data-dumps (the `pbi_*` sheets). On IPV this is ~6
   real sheets out of 27.
2. **Per-sheet understand (map, parallel)** — each agent gets the sheet view
   (grid + image) + a small shared workbook brief, and emits grounded,
   schema-constrained JSON: label column, metric rows + hierarchy, periods,
   input cells, sections (typed + free-text purpose), metric identities +
   units/sign, visible author rules — each field with `evidence` (cell refs)
   and `confidence`.
3. **Synthesize (reduce)** — one call sees all per-sheet outputs **+ the
   dependency graph + named ranges** → reconciles metrics across sheets, maps
   the valuation data-flow, names the input surface + archetype, derives
   business/covenant rules + impact narratives.
4. **Verify (adversarial)** — checks each claim's citations against the
   snapshot, scores confidence, flags the uncertain for human review.
5. **Compile** — write the grounded structure to the tables (below).

## Model tiering (see §API for IDs/pricing)

**DECIDED: every stage on Opus 4.8** (`claude-opus-4-8`, $5/$25 per 1M) —
max quality + consistency, and Opus's high-res vision (2576px) is exactly what
the messy sheets need. Routing is trivial work but stays on Opus for simplicity.

All thinking is **adaptive** (`thinking:{type:"adaptive"}`, `effort:"high"`);
`budget_tokens`/`temperature` are removed on Opus 4.8.

## API mechanics (grounded in the current Claude API)

- **Vision:** image content blocks (base64 or Files-API `file_id`); multiple
  images per request. Opus 4.8 max 2576px long edge (~4,784 tokens/full-res
  image). Upload each sheet PNG once via the Files API and reference by
  `file_id` to avoid re-encoding across retries.
- **Structured output:** `output_config:{format:{type:"json_schema",schema}}`
  (or `client.messages.parse()` with a Pydantic model — recommended). Schema
  limits: **no recursion** (so the section tree is a flat list with
  `parent_section_id`, exactly like our metric rows), `additionalProperties:false`,
  no numeric/length constraints (validate those client-side). Supported on Opus
  4.8 / Sonnet 4.6 / Haiku 4.5.
- **Prompt caching:** put the shared **workbook brief** (sheet list, named
  ranges, archetype hints, instructions) first with `cache_control:{ephemeral}`
  so it's reused across per-sheet calls. Min cacheable prefix on Opus 4.8 =
  4,096 tokens; reads ~0.1×, writes ~1.25×. Pre-warm once, then fan out (a
  cache entry is only readable after the first response streams).
- **Batch API (optional):** 50% cheaper, async, completes < 1h — ideal for the
  per-sheet fan-out (it's a background job). Trade-off: concurrent batch
  requests can't share a freshly-written cache, so it's *either* batch-50% *or*
  cache-reuse; at ~6 sheets both are cheap.

## What it writes (schema — migration `0004`)

Layer 3 **populates** the structure tables as the authoritative, grounded
source (deterministic detectors become *input signals*, not the truth). Keeps
layers clean by separating LLM interpretation from deterministic facts:

- `template_metric_rows` / `template_fields` — gain a `source` (`deterministic|llm`),
  `confidence`, `evidence jsonb`. The per-sheet agent writes the real rows
  (correct label column, real hierarchy) for messy sheets.
- `template_sections` *(new)* — the **final** LLM-assigned sections:
  `sheet_id, title, section_type (closed taxonomy), purpose, cell_range,
  parent_section_id, confidence, evidence`. (Layer 2's `template_section_signals`
  feed this.)
- `template_metric_identities` *(new)* — `metric_row_id, canonical_metric,
  metric_type, value_role, unit, sign_convention, confidence, evidence`. LLM
  semantic layer over the structural rows (incl. the Reported/Adjusted/Covenant
  nuance).
- `template_author_rules` *(new)* — `rule_category (closed taxonomy),
  source_type, source_location, raw_text, summary, is_strict, affects jsonb,
  confidence`.
- `template_understanding` *(new, workbook-level)* — `archetype, purpose,
  audience, data_flow_narrative, input_surface_sheet, sheet_roles jsonb,
  confidence`. This is the **core brief** the future query-time agent loads.

Permissive-MVP RLS like prior migrations; written by the parser (service-role).

## Cost (IPV, ~6 meaningful sheets, Opus 4.8)

Per sheet ≈ grid+image+brief ~12K in / ~3K out ≈ **$0.14**; ×6 ≈ $0.85. Plus
synthesis (~$0.25) + verify (~$0.20) + routing (cents) ≈ **~$1.50/template**,
or **~$0.75 with the Batch API**. Affordable per onboarding.

## Risks & mitigations

- **Hallucinated structure** → citations validated against the snapshot; cells
  that don't exist are rejected; graph/impact stay deterministic.
- **Inconsistency across runs** → schema-forced output, `effort:high`, low
  variance; human review locks the contract.
- **Cost/latency** → routing prunes to ~6 sheets; batch + caching; background job.
- **Big sheets exceed context** → tile the grid; image per tile; `Quarterly_Output`
  (52K cells) is the main case.

## Phasing

1. **Sheet-view builder** (grid + Aspose image) from the snapshot — reusable, testable.
2. **Per-sheet agent** (schema, grounding/citation contract) → writes metric
   rows/fields/sections/identities for the 0-row sheets. *Prove on `PortCo_Input`
   + `Multiple` first.*
3. **Synthesize + verify** → `template_understanding`, author rules, reconciliation.
4. **Migration 0004** + persistence + endpoints (`/understand/{id}`, read APIs).
5. Tests (sheet-view rendering, schema validation, citation-validator, a recorded-fixture LLM pass).

## Decisions (signed off)

- **All stages on Opus 4.8.** Max quality/consistency + high-res vision.
- **Live calls + prompt caching** for the per-sheet fan-out (interactive
  "Analyze"; cache the shared workbook brief, pre-warm once then fan out).
- **Prerequisite:** the parser service needs `ANTHROPIC_API_KEY` in `parser/.env`
  to call Claude (alongside the Supabase + parser keys already there).
