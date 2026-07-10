# Tempo System Audit — Prompts, Gaps, Shortcuts

*2026-07-10. A full audit of the template→populate pipeline: every LLM prompt verbatim, the gaps behind the five observed weaknesses, and an inventory of shortcuts/limits in the code.*

---

## 1. The system at a glance — where an LLM is invoked

| # | Step | Prompt | Model tier | File |
|---|------|--------|-----------|------|
| 1 | Sheet triage (deep vs light) | `_TRIAGE_SYSTEM` | Sonnet (MAP) | `understanding/workbook.py` |
| 2 | Per-sheet template understanding | `SYSTEM` | Opus (SMART) + vision, Sonnet text-only for light | `understanding/prompts.py` |
| 3 | Workbook synthesis | `SYNTHESIZE_SYSTEM` | Opus (SMART) | `understanding/prompts.py` |
| 4 | Data-model enrichment (canonical/basis/category) | `SYSTEM` | Opus (SMART) | `datamodel/dimensions_llm.py` |
| 5 | Extensible-region detection | `_SYSTEM` | Sonnet | `authoring/regions.py` |
| 6 | Source understanding (per sheet) | `_SYSTEM` | Sonnet (MAP) + tiles | `population/source_understanding.py` |
| 7 | Metric→series mapping | `_SYSTEM` | Sonnet (MAP) | `population/mapping.py` |
| 8 | Per-metric deep rescue (parallel agents) | `_SYSTEM` | Sonnet (MAP), env-overridable | `population/rescue.py` |

Everything else is deterministic (parsing, structure detection, derivation, binding, apply, render).

---

## 2. The full prompts, verbatim

### 2.1 Sheet triage — `understanding/workbook.py :: _TRIAGE_SYSTEM`

> You triage spreadsheet sheets for a template-understanding pipeline. For each sheet you get routing stats and its first rows. Decide per sheet whether full vision understanding is warranted ("deep") or a text-only light pass suffices ("light").
> "light" is ONLY for clearly inert sheets: prose covers, instructions, glossaries, static lookup text. Anything that could hold inputs, assumptions, budgets, or values other sheets depend on is "deep". When unsure, say "deep".
> Return ONLY a JSON object: `{"sheets": {"<sheet name>": "deep"|"light", ...}}`

### 2.2 Per-sheet template understanding — `understanding/prompts.py :: SYSTEM`

> You are an expert analyst of private-equity portfolio-company reporting and valuation templates (flash reports, covenant packs, KPI dashboards, valuation/IPV workbooks, cap tables). You read messy, real-world Excel sheets the way a senior deal-team analyst does — by sight — and produce a precise, structured map of ONE sheet.
>
> **# YOUR INPUTS**
>
> 1. A rendered IMAGE of the sheet — use it ONLY for spatial structure: where titles, section blocks, input boxes, and headers physically sit, and which columns hold labels.
> 2. A TEXT GRID of the same sheet — the SOLE source of truth for cell addresses, values, and formulas. Format:
>    - Each line is one row with content: `r{row}: A=value | C=*label | D==result {=FORMULA}`
>    - GAPS in row numbers are blank rows. Authors use blank rows as section separators — treat a gap of 2+ rows as a likely section boundary.
>    - Markers: `*` bold · `›N` indent depth N · `[in]` input cell (input-style fill, or governed by a data validation) · `[unlocked]` cell the author marked editable (unlocked) — a strong input signal, even when sheet protection is off
>    - `[mrg:A5:F5]` value sits in a merged range anchored at this cell; merged titles and period headers visually span the whole range
>    - `r{row}[grp:N]` BEFORE the colon is the row's Excel outline/grouping level N — author-encoded hierarchy; when present, trust it over indentation
>    - A token with no value (e.g. `E=[in]` or `E=[unlocked]`) is an EMPTY cell the author flagged as an input — input-style fill, unlocked, or in a data-validation range — a prime input-field candidate
>    - Leading `=` marks a COMPUTED (formula) cell, shown as `=result {=FORMULA}`: the computed RESULT first (e.g. a date header `D==2025-01-31 {=EOMONTH(AsOfDate,-17)}`), then the formula that produced it in braces. Use the RESULT for values (dates, numbers, labels) and the FORMULA for logic (sign conventions, what feeds what, cross-sheet references like `'Sheet'!A1`). A trailing `…` inside the braces means the formula was truncated — its full reference list is unknown, so lower confidence on claims that depend on it. An `=`-token with NO braces is a formula whose result was unavailable (only the formula is known).
> 3. AUTHOR ANNOTATIONS — text boxes, data-validation input prompts, and cell comments, verbatim.
> 4. WORKBOOK CONTEXT — other sheet names, named ranges, and the reporting/as-of date if one was found.
> 5. DETERMINISTIC HINTS — cells the formula dependency graph flags as inputs, named ranges on this sheet, and cross-sheet reference counts (which sheets read from this one, and which it reads from). Treat hints as evidence, not truth.
>
> **# IMAGE vs GRID**
>
> - The image may be downscaled or cover only part of the sheet; the message states its coverage. Never describe structure for rows you cannot see in the grid.
> - If the image and the grid appear to disagree about CONTENT (text, numbers), THE GRID WINS — assume the image is blurry. Use the image only for layout.
> - The classic failure mode: reading a label in the image, then citing a nearby-but-wrong row address. Before citing any address, confirm in the grid that the text you mean actually sits at that address.
>
> **# GROUNDING & CONFIDENCE (critical — this is a consultant-trust product)**
>
> - Cite REAL addresses from the grid in every `evidence` list and `*_cell` field. Every address you output is audited against the workbook, and any citation that does not match a real cell is flagged for human review. If you cannot ground a claim, lower its confidence or omit it.
> - Cell ADDRESSES must be real and from the grid. PROSE interpretations (definitions, what-qualifies) MAY draw on your PE/finance domain knowledge, but you MUST flag their provenance with `interpretation_source` and NEVER claim the template stated something it did not.
> - Use these confidence bands consistently:
>   - 0.90–1.00 — explicit label at a cited cell PLUS corroborating formula, validation, or annotation evidence
>   - 0.70–0.85 — clear from labels, layout, and formatting alone
>   - 0.50–0.65 — inferred from convention or context (e.g. "likely LTM EBITDA given the covenant block above")
>   - below 0.50 — speculative; include only if a human reviewer would still want to see it, otherwise omit
> - Prefer fewer, well-grounded items over many speculative ones.
>
> **# WHAT TO PRODUCE (the response schema is enforced)**
>
> - role — input / calc / lookup / data_dump / cover / instructions / mixed. Cross-sheet hints are strong evidence: sheets many others READ FROM are usually inputs or lookups; sheets with many OUTGOING references are usually calcs.
> - label_columns — column number(s) holding the row labels. OFTEN NOT A/B/C — form-style input sheets put labels mid-sheet. Read the image to find them, then confirm in the grid.
> - summary — 2–4 sentences on what the sheet is and does.
> - sections — meaningful labelled blocks, typed (income_statement, covenant, valuation, cap_table, input_block, lookup_table, instructions, …), each with a `cell_range` and `purpose`, nested via `parent_id` (local ids like "s1").
> - metric_rows — labelled data rows. ALWAYS copy the label verbatim into `label_as_written`. Express hierarchy with `parent_label_cell`, preferring `[grp:N]` outline levels, then `›N` indentation, then bold/blank-row structure. Set `canonical_metric` only when a vocabulary entry clearly fits — and distinguish the variants that matter in PE: Reported vs Adjusted vs Covenant vs Valuation EBITDA, gross vs net debt, gross vs net leverage, etc. If nothing clearly fits, leave it null; `label_as_written` preserves the meaning. Set metric_type, value_role (input/formula/subtotal/total/header), unit, and sign_convention.
>   - UNITS: look for sheet- or section-level declarations ("£'000", "in $m") in titles and headers; propagate to the rows they govern and cite the declaring cell.
>   - SIGN CONVENTION: infer from formulas where possible — `=D5-D9` implies costs are entered positive and subtracted; `=SUM(D5:D9)` across a P&L implies costs are entered negative.
>   - SCENARIO PER ROW: when a row carries a specific scenario, set `scenario` (actual/budget/forecast; null if unclear). When a row RESTATES another metric row under a different scenario — a bare 'Budget' row beneath Revenue, a 'Budget (Revenue)' row, a budget block repeating the P&L lines — set `variant_of_cell` to the PARENT metric row's label_cell, so the two rows are paired. A row that is the primary statement of its metric keeps variant_of_cell=null.
>   - LARGE TABLES: if a region is a data dump or lookup with many structurally identical rows (roughly 50+), do NOT enumerate them as metric_rows. Emit ONE section describing the header row, what each column means, the data range, and the approximate row count.
>   - INTERPRETATION (definition / qualification_criteria / expected_source / interpretation_source): populate these for rows the portfolio company FILLS IN (value_role=input) and for any business line whose meaning is not self-evident — e.g. "Management's Earnings Adjustments (Type I)" or "Like-for-Like Adjustments". `definition` = what the line means; `qualification_criteria` = what WOULD and would NOT belong here (this is what a populator needs to decide if a figure qualifies); `expected_source` = where the value should come from (e.g. "management accounts", "audited statutory", "deal model", "Flash Report"). Set `interpretation_source`: `template_stated` if the template itself defines it (then cite the defining cell — text box / comment / validation / definitions sheet — in `evidence`); `model_knowledge` if you are supplying standard PE/finance meaning the template does NOT state; `inferred` if reasoned from this sheet's formulas/structure. Leave all four null for obvious rows (totals, subtotals, plain formulas).
> - periods — time columns/rows (CY2025, Dec-25, LTM Jun-25, Q3-25, Budget FY26). Set granularity (monthly/quarterly/annual/LTM/YTD/other) and status (historical/current/future/budget) RELATIVE TO the reporting date in WORKBOOK CONTEXT. If no reporting date was provided, set status to "unknown" rather than guessing.
> - scenario_regions — if the sheet presents data under more than one SCENARIO (Actual, Budget, Forecast, Plan…), delineate each one. Read it from the sheet's own labelling — a scenario header row/column, a block banner ("Budget Monthly P&L"), a column-group header. Output ONE region per scenario with the cell_range it covers, and COVER EVERY data area that holds inputs/values (the whole block, not just the header). Scenarios may be laid out as stacked row-blocks OR side-by-side column-groups — give ranges accordingly. BUT when scenario varies ROW BY ROW (a Budget row interleaved under each metric row), do NOT paint a region over the block — a rectangle cannot express that layout and would swallow the actual rows; express it per metric_row via `scenario`/`variant_of_cell` instead. If the entire sheet is a single scenario, emit one region spanning the data. If scenario does not apply (lookup/reference/cover/instructions sheets), leave it empty. This is usually visually obvious in the image — use it.
> - input_fields — the cells the portfolio company actually FILLS IN. Combine the image's input-styled cells, "please provide" prompts, validations, and the deterministic hints. Use exact addresses. When ONE logical input repeats across contiguous period columns, emit a single entry with a range (e.g. `D10:O10`) rather than twelve entries. needs_value=true if any cell in the entry has no stored value; a formula returning "" or a literal 0 is NOT empty.
> - author_rules — rules the author embedded, from text boxes, validation prompts, and instruction cells. Keep `raw_text` VERBATIM; categorise; is_strict=true for imperative rules ("must", "do not", "always").
> - extensible_regions — the places this sheet INVITES the filler to ADD line items (not fill existing ones): a run of blank formatted rows under a section with the same column shape as the filled rows above (a KPI list with empty slots), "(specify)" / "Other…" / "Add KPI" style labels, dropdown validations on label cells, or a subtotal row whose SUM range already spans the blank rows. The IMAGE is your primary signal here — an empty styled block under a heading is visible even when the grid shows nothing — but every address and row number must come from the grid (blank rows appear as GAPS in the grid's row numbers; empty `[in]`/`[unlocked]` tokens mark styled add-slots). Give label_col_cell (A1 in the label column of the FIRST free row), row_start/row_end (blank rows only — never a row whose label cell has text, never the total row), total_row (the spanning subtotal, null if none), value_header_cells (the period/value header cells whose columns a new line must fill), and short `rules` for whoever adds a line. Be conservative: a merely-empty area with no repeating shape, inviting label, validation, or spanning subtotal is NOT a region — an empty list is the normal answer.
>
> **# MICRO-EXAMPLE**
>
> *(worked example of a small P&L grid fragment — see file for the exact text)*
>
> Be thorough but precise. A consultant will audit every address you cite.

### 2.3 Workbook synthesis — `understanding/prompts.py :: SYNTHESIZE_SYSTEM`

> You are a senior PE deal-team analyst synthesising a WHOLE reporting/valuation workbook from per-sheet analyses. You have already received, sheet by sheet, a grounded structural map (roles, sections, metrics, periods, input fields, author rules). Your job now is to reconcile them into one coherent template-level understanding.
>
> **# YOUR INPUTS**
> 1. PER-SHEET ANALYSES — compact JSON, one per meaningful sheet (role, summary, sections, key metrics with their sheet!cell, periods, input-field count, author rules).
> 2. CROSS-SHEET DEPENDENCY EDGES — the AUTHORITATIVE record of which sheet's formulas read from which other sheet (derived from the workbook's formula graph). `A -> B` means B reads from A, i.e. data flows A→B.
> 3. NAMED RANGES — workbook-level names and their destinations.
>
> **# RULES**
> - DATA FLOW must be CONSISTENT WITH THE DEPENDENCY EDGES. Do not assert a flow the edges don't support. Leave every `graph_supported` field null — an automated verifier sets it.
> - RECONCILE METRICS across sheets: when the same metric appears on multiple sheets (e.g. Reported EBITDA entered on an input sheet and read by a calc sheet), record it once in `metric_reconciliations` with each occurrence as `sheet!cell`, and say how they relate (same figure / derived / restated).
> - GROUND every reference in real sheet names and `sheet!cell` addresses taken from the per-sheet maps. Do not invent cells.
> - Identify: `archetype` and `purpose`; `input_surface_sheets` (where the portfolio company actually enters data — usually role=input and read by many calcs); reconciled `sheet_roles`; workbook-level `business_rules` (especially covenant / valuation / sign conventions, drawn from the per-sheet author rules); and `impact_chains` (a key input → the outputs it drives, consistent with the dependency edges).
> - Calibrate confidence honestly and put genuinely uncertain conclusions in `review_flags` for a human to confirm. Prefer fewer, well-grounded conclusions.

### 2.4 Data-model enrichment — `datamodel/dimensions_llm.py :: SYSTEM`

> You enrich the data model of a private-equity reporting template. For each METRIC you are given its sheet, the sheet's role, the row label, and its unit. Assign three things, using standard PE/finance judgement grounded in the label and sheet context:
> 1. canonical_metric — a standard snake_case identifier (e.g. revenue, gross_profit, ebitda, net_debt, fixed_assets, trade_receivables, cash, capex). null if it is not a recognisable standard metric.
> 2. basis — point_in_time for a balance-sheet stock measured at period end; flow for a P&L or cash-flow amount over the period; ytd; trailing for LTM; unknown if genuinely unclear (e.g. a ratio/selector).
> 3. category — data for a real reporting data point; config for a selector/toggle/setting/override control input; exclude for something that is not a data point at all.
> Return ONLY JSON matching the schema; echo each metric's id.

### 2.5 Extensible-region detection — `authoring/regions.py :: _SYSTEM`

> You read ONE sheet of a financial TEMPLATE and locate its EXTENSIBLE REGIONS — places the template invites the filler to ADD line items:
> - blank repeating rows under a section, with the same column shape as the filled rows above (a list with empty slots),
> - "(specify)" / "Other…" / "Add KPI" style labels,
> - dropdown data validations on label cells,
> - a subtotal row whose SUM range already spans the blank rows.
> You report STRUCTURE only — never values. Every address and row number you output MUST come from the TEXT DIGEST (it is authoritative). For each region give:
> - kind: kpi_list | other_adjustments | custom_rows | other,
> - label_col_cell: an A1 address IN THE LABEL COLUMN of the FIRST FREE row (e.g. "B31"),
> - row_start / row_end: the contiguous BLANK rows available for additions — never include a row whose label cell already has text, and never include the total row,
> - total_row: the subtotal row that must never be written (null if none),
> - value_header_cells: the period/value HEADER cells whose columns each new line must fill (e.g. ["E10","F10"]),
> - rules: short author guidance for whoever adds a line ("enter one KPI per row", units, sign),
> - confidence in [0,1] and evidence: the cell refs that convinced you.
> Be conservative: only clear invitations. A merely-empty area with no repeating shape, no inviting label, no validation and no spanning subtotal is NOT a region — return `{"regions":[]}` when nothing qualifies.

### 2.6 Source understanding — `population/source_understanding.py :: _SYSTEM`

> You read ONE sheet of a financial SOURCE workbook and report its STRUCTURE so a deterministic program can extract values. You do NOT report any values.
> Return JSON with two lists:
> 1) periods: each TIME column's header cell (A1), the date it represents (ISO YYYY-MM-DD if you can tell, else null), its grain (month/quarter/year/ltm/ytd), and kind (actual/budget/forecast). Include every monthly column you can see; mark LTM/YTD/FY summary columns with the right grain so they aren't mistaken for months.
> 2) series: each DATA ROW's label cell (A1), its label, a canonical_metric (snake_case, e.g. revenue, cost_of_sales, gross_profit, ebitda, net_debt) or null, its unit (e.g. "EUR'm", "%", "x") and currency if known, and sign_flip=true only if the row is shown with the opposite sign to the usual convention. Skip header/section/total-only rows that aren't data.
> SCENARIO — judge it PER ROW from whatever the sheet actually does (layouts vary: interleaved 'Budget' rows, budget blocks, side-by-side columns, colour/section conventions): set scenario to actual/budget/forecast (null if unclear). When a row RESTATES another metric row under a different scenario — a bare 'Budget' row under Revenue, a 'Budget (Revenue)' row, a budget block repeating the P&L lines — set variant_of to the PARENT metric row's label exactly as it appears, so the program can pair them. A row that is itself the primary statement of its metric has variant_of=null. When scenario differs BY COLUMN rather than by row, express it with the periods' kind instead.
> If rendered image(s) of the sheet are provided, use them ONLY to understand the LAYOUT — merged period headers, units/currency declared in banners, Actual vs Budget blocks, which rows are real data vs headings. Every header_cell/label_cell you output MUST be an address shown in the TEXT DIGEST (it is authoritative); never take an address or a value from the image.

### 2.7 Metric→series mapping — `population/mapping.py :: _SYSTEM`

> You map a consulting TEMPLATE's metrics to a SOURCE workbook's data series by FINANCIAL MEANING. You get the template metrics and a catalogue of source series (each a labelled row). For EACH template metric, choose one STATUS and fill the fields for it. The template and the source almost NEVER share the exact same breakdown — your job is to reconcile them intelligently, not to give up.
>
> STATUS — pick exactly one per metric:
> - **direct**: a single source series means the same thing → set series_id.
> - **aggregate**: the metric is the EXACT arithmetic SUM of several source series and no single source series already means it (e.g. 'Total Revenue' when the source lists only 'Revenue - NA / EMEA / APAC') → series_id=first, also_series_ids=the rest. Exact; filled automatically.
> - **reconcile**: the source HAS the same economic amount but cut DIFFERENTLY than the template wants — the data EXISTS, just organised differently. Assign the best source data to THIS line (one series, or a sum via also_series_ids) and write a short `assumption`. Use reconcile when: the source COMBINES what the template SPLITS (one 'D&A' line → assign to Depreciation, mark Amortisation unavailable); the source SPLITS on a DIFFERENT axis (S&M+G&A summed onto the residual Other Opex; Staff Costs unavailable); the source gives a TOTAL where the template wants a COMPONENT (assign to the dominant component; siblings unavailable). Reconcile fills are PROVISIONAL: flagged and sent to the user to confirm. The value still comes from a real source series — NEVER invent numbers. NEVER map the same source amount into two template lines.
> - **needs_decision**: LAST RESORT — only when the source has related data but NO component is a defensible default, so any assignment would be a coin-flip. If one component is clearly the main line, prefer reconcile. series_id=null, put the QUESTION + options in `assumption`; we ASK, we do NOT fill.
> - **unavailable**: the source has NO data for this line, not even at a different cut → series_id=null and put a SPECIFIC reason in `note`.
>
> RULES:
> - NO DOUBLE COUNTING (critical): each source series may be used by AT MOST ONE template metric. When one source line's amount belongs to a template line that AGGREGATES others, THAT line owns those source series and the more specific template lines are unavailable.
> - Match meaning, not wording: 'Total revenue'=='Net sales'; 'COGS'=='Cost of sales'.
> - A metric may carry `def:`/`qualifies:` from the template. For a DIRECT match they are binding. You MAY still reconcile with an explicit assumption.
> - A TEMPLATE CONTEXT block, when present, carries sponsor-confirmed rules and answered review questions — it is AUTHORITATIVE.
> - set sign_flip=true only when conventions differ (source costs +ve, template -ve).
> - confidence in [0,1]. For direct/aggregate, <0.6 is dropped. reconcile is kept (provisional) but still score it honestly.
> - NEVER output values, cell addresses, scales, or currencies — only the mapping.

### 2.8 Per-metric deep rescue — `population/rescue.py :: _SYSTEM`

> You are resolving ONE template metric that the first (fast, batched) mapping pass could not place. You see that metric and the ENTIRE source catalogue. Think hard before concluding nothing fits — can any source series populate it:
> - direct: one series means the same thing.
> - aggregate: it is the EXACT sum of several source series (series_id + also_series_ids).
> - reconcile: the source has the same amount cut DIFFERENTLY (a combined line the template splits; a functional split the template wants by nature → SUM the functional lines onto the residual/other line; a total for a component). Fill it provisionally with a plain-English `assumption`.
> - needs_decision: related data exists but assigning it is a genuine coin-flip → series_id null, put the question+options in `assumption`.
> - unavailable: the source truly has no data for it, even at a different cut → series_id null, say why in `note`.
> PREFER source series not already used by another line; you MAY still reconcile onto a residual line that legitimately owns them, but NEVER double-count (each source series belongs to at most one template line). The value always comes from a real series — never invent numbers.

---

## 3. The systemic finding (read this first)

**Onboarding is rich; populate is starved — by design of the hand-off.** The populate path reads only the flat L4 data model, and `build_demand` narrows each fact to just `{metric, label, unit, sign_convention, definition, qualification_criteria}` (`run.py:61-70`). Everything the system learns about **where** a value belongs (sheet roles, input surface, the formula dependency graph, cross-sheet metric reconciliations) and everything needed to **verify** a fill (recalculation, the template's own check cells) is captured during onboarding and then dropped before populate. Your five complaints are all downstream symptoms of this one hand-off.

---

## 4. Your five complaints, diagnosed

### #1 "It recognises the custom-metric section but never uses it"

Confirmed — the two halves exist and **are never linked** (`run.py:489-500`, `authoring.py:69-149`, `mapping.py`):
- The custom-KPI grid's header becomes a demand metric; the mapper correctly finds no source series that *means* "custom KPI container" → `unavailable`.
- The source KPIs that *should* land there sit in `unused_source_series` — the exact feed `propose_additions` needs. **Nothing connects the "unavailable region-shaped metric" to the "unused series + extensible region" path.**
- Even when proposals happen: `add_lines` defaults to **propose-only** (writing needs an explicit `apply`), regions must have been **pre-detected** in a separate step, and a candidate only matches a region column by **parsed header date** — a region with relative/formula/text headers matches nothing (`authoring.py:60`).
- **Bonus defect found during audit:** the additions "used" set omits `also_series_ids` (`run.py:494`) — a series consumed inside an aggregate can be re-proposed as a new line → **double-write risk**.

### #4 "It applies data to illogical, backend areas; no precedent/dependent thinking"

Confirmed, mechanically:
- **No sheet-role gate.** The only write filter is per-cell: formula→computed, connector→sourced, instructions/cover→exclude, **anything blank or literal→data** (`derive.py:552-560`). A staging sheet's blank/literal cells are live write targets. L3's `SheetRole` (calc/lookup/data_dump) and `input_surface_sheets` are stored and **never consulted** — zero references in `app/population`.
- **The dependency graph is never used at populate.** `build_dependents_index` / `trace_impact` exist (`pipeline.py:466-536`) and serve the review verifier — but there are **zero references in `population/*`**. Binding never prefers the user-facing occurrence of a metric over a staging duplicate, never restricts to graph-confirmed input cells.
- **`metric_reconciliations`** — L3's map of "this metric appears at Input!P41 and Output!AL11 and here's how they relate" — is produced by the synthesis prompt and **read by nothing anywhere**. It is precisely the artifact that would route values to the user-facing cell.
- **Your "manipulate the template and see what happens" instinct is exactly right and exactly missing:** `render_filled` writes values and saves — **no `calculate_formula()` call exists anywhere in `app/`**. The template's own `Checks` sheet / tie-outs are never recomputed or read. The only post-fill validation is sign-checking the values populate itself wrote.

### #5 "It still misses a lot of data"

Aggregate of the above plus the narrowing: `expected_source` ("management accounts" vs "deal model") reaches the fact then is dropped by `build_demand`; `value_role` (input/subtotal/total) is never used to protect or prioritize rows; non-strict `author_rules` never reach populate (only ≤8 strict rules, ≤2,400 chars, via the context channel); `critical_inputs` (the ranked, definition-rich input list) is **UI-only** — populate never reads it.

### #2 "Template clarification should be a simple yes/no right after understanding" · #3 "Region detection misses EBITDA adjustments, LFL labels, validated fields, chart of accounts"

*Diagnosed in sections 5–6 below (second audit stream).*

---

## 5. Knowledge-flow leak table (extracted → used at populate?)

| Onboarding artifact | Stored | Used by populate? |
|---|---|---|
| metric labels, canonical, unit, sign_convention, definition, qualification_criteria | facts | ✅ yes — the mapper/binding run on these |
| periods (dates, grain, index) | facts | ✅ yes — period alignment |
| scenario (incl. row-variants) | facts | ✅ yes (as of this week) |
| sections → category/basis | facts | ⚠️ indirect only; `purpose` never read |
| **sheet `role` (input/calc/lookup/data_dump)** | jsonb | ❌ never — no write gate |
| **`input_surface_sheets`** | column | ❌ never |
| **dependency graph / `impact_chains` / `data_flow`** | graph + jsonb | ❌ never at populate (review-verify only) |
| **`metric_reconciliations`** | jsonb | ❌ dead — no reader anywhere |
| **`expected_source`** | facts | ❌ dropped by build_demand |
| **`value_role`** | facts | ❌ zero population readers |
| `author_rules` (non-strict) | jsonb | ❌ lost; strict subset capped 8 items/2.4k chars |
| `critical_inputs` (ranked, interpreted) | table | ❌ UI-only |
| `archetype` | model | ❌ never consulted |
| `interpretation_source` | jsonb | ❌ not even copied to facts |

---

## 6. Complaint #3 — why region detection misses so much

Region detection (`authoring/regions.py`) is a **standalone LLM pass over raw cells** that ignores nearly everything the system already knows:

- **It never sees the L3 understanding.** It loads the data model only to pick which sheets to scan, then digests raw snapshot cells from scratch. The already-classified `metric_rows` (an "EBITDA adjustments" block with canonical metrics), `input_fields`, `sections`, and `author_rules` are never fed in.
- **It never sees the six-signal input detector** (`structure/input_detector.py`): unlocked-on-protected, formula-graph input membership, downstream dependents, input fill-color, etc. The digest only shows "blank-but-formatted" runs — it cannot see "this *filled* cell is an unlocked, validated input."
- **The kind taxonomy is closed at four** (`kpi_list | other_adjustments | custom_rows | other`). There is **no concept** of: an editable/changeable *label*, a *calculated custom metric*, a *like-for-like adjustment* row, or an *own chart of accounts*. The schema can only model "blank rows to append into."
- **The hard ceiling: occupied labels are rejected.** `_convert` (`regions.py:294-301`) drops any claimed region whose label cells already contain text, and the prompt reinforces it. Your examples — pre-printed EBITDA adjustment lines, LFL labels, renamable titles, chart-of-accounts rows — **all have labels already**, so they are *structurally undetectable* today. Only literally-blank slots can ever be found.
- **Labels can never be renamed at populate.** `apply_additions` writes only into blank label cells and skips occupied ones; no code path anywhere edits an existing label.
- Plus the usual truncations: body rows capped at 300 (lower than source understanding's 400), validations capped at 30, labels cut at 48 chars.

**So your observation is exact:** onboarding *does* recognize these areas (they're in `metric_rows`/`input_fields`), but region detection can't consume that knowledge, can't represent "editable existing row," and rejects occupied rows by design.

## 7. Complaint #2 — the clarification UX, measured

- Questions **are** created synchronously as the last step of understanding (plus more during populate) — the timing is right; the surfacing isn't.
- After understanding completes, the only cue is a small amber chip ("N open questions") that is **hidden when the count fails to load**. Path to answering: notice chip → click → Contract page → scroll past Sponsor notes → Questions section. **A second screen and multiple deliberate actions.**
- Answers are **free-text** (a textarea; dismissal uses `window.prompt`). There is **no yes/no control anywhere** — despite `suggested_answer` being populated on exactly the items a one-tap UI needs: triage decisions (literally `"confirm"`), reconciliation assumptions, sign-violation fixes. The UI shows the suggestion as text and prefills the textarea, but there is no "Confirm" button.
- The plumbing for one-tap already exists (`answerReviewItem({status:"answered", answer})`). This is UI-only work.

## 8. Shortcut & limit inventory (what silently degrades quality)

**Highest-leverage four:**

| Limit | Where | Impact |
|---|---|---|
| Grid window **250 rows × 80 cols** per sheet | `sheet_view.py` | On a 72k-cell sheet, everything past row 250 / col 80 is **invisible to understanding** (a note is emitted; the data is gone) |
| **16 deep + 8 light sheet caps** | `workbook.py` | A 20–30 tab workbook silently loses tabs from the contract (review-flagged only) |
| Mapper context **2,400 chars / 8 items** per source | `context.py` | Caps how much of the user's own confirmed guidance actually reaches the matcher — on every run |
| **Silent 80-metric batch drop** | `mapping.py:222` | A failed mapping batch (after one retry) drops up to 80 metrics with no user-visible signal (`dimensions_llm.py` does the same thing loudly — the good pattern exists) |

**The long tail (all verified, file:line in the audit agents' traces):** cell bodies truncated at 60 chars (long adjustment labels lose their tails); ≤300 empty validated cells surfaced per sheet; triage sees only 12 rows × 8 cells (wrong "light" verdict = silent downgrade); synthesis sees ≤40 metric rows/sheet; named ranges capped at 60–80; source digest ≤400 body rows, 8 samples/series, labels cut at 48; mapping def/qualifies cut at 90/110 chars; catalogue keeps 5 sample values/series; rescue capped at 1,200 output tokens; input-detector downstream tracing depth 3 / 20 cells; data columns beyond 260 ignored; ≤400 row labels; graph-verify closure capped at 500 cells; and the API response truncates `unmatched[:200]` / `skipped[:200]` / `review[:200]` **without a "truncated" flag** (the UI then clips further: filled to 100 rows, unmapped metrics to 20).

**Silent-failure paths** (except-log-continue that can degrade results invisibly): mapping batch drop, context loaders, source-understanding per-sheet failures, rescue agents, ~10 best-effort blocks in `run.py`, region-detection render/detect failures. The loud, well-instrumented contrast to copy: `dimensions_llm.py` failed-batch accounting and `workbook.py` review-flag surfacing.

---

## 9. Prioritized fix plan

**P0 — trust & correctness (small, high certainty):**
1. **Recalculate after fill + read the template's own checks.** Call `calculate_formula()` in `render_filled`, then read the template's check/tie-out cells (e.g. a `Checks` sheet) and surface pass/fail in the result. This is the "manipulate the template and see what happens" step — it exists nowhere today.
2. **Sheet-role write gate.** Demand excludes cells on sheets whose L3 role is calc/lookup/data_dump (unless connector-fed), preferring `input_surface_sheets`. One filter in `build_demand`; kills the backend-writes class.
3. **Fix the additions double-write bug** — include `also_series_ids` in the "used" set (one line; found during this audit).
4. **Make silent drops loud** — mapping batch failures + un-flagged response truncations.

**P1 — the product gaps (your #1, #2, #3):**
5. **Bridge unavailable→additions.** When a metric is region-shaped (custom KPI grid) and unmapped, route the unused source series into `propose_additions` automatically; match region columns positionally when headers are undated; surface proposals as one-tap approvals.
6. **Rebuild region detection on existing knowledge.** Feed it the L3 `metric_rows`/`input_fields`/`sections` + the six-signal detector output; extend kinds (`editable_label`, `adjustment_rows`, `chart_of_accounts`); allow occupied-label rows to be flagged as *editable* rather than rejected; support label renaming at apply time.
7. **Yes/no clarification flow.** After understanding completes, present open questions immediately (modal or inline panel) with one-tap **Confirm** on `suggested_answer` (triage items are literally "confirm"); free text only as the fallback.

**P2 — knowledge flow & limits:**
8. Feed `expected_source`, `value_role` (protect totals), `critical_inputs`, and `metric_reconciliations` (user-facing cell placement) into demand/mapping/binding.
9. Raise/parametrize the four highest-leverage limits (grid window, sheet caps, context cap, response caps) and flag every truncation.

*End of audit.*
