# Migration Document: Template Onboarding & Contract Creation

> **Purpose:** Plan for porting the existing Aspose-based parser into the new
> layered architecture (raw extraction → structure → interpretation → contract).
> **Status:** Planning / reference.
> **Last updated:** 2026-06-09
>
> _Note: the wide tables in the original paste were truncated in places. Where
> source text was clearly cut off mid-word, it is marked with `[…]`. Nothing has
> been invented to fill the gaps — re-paste the original if a `[…]` cell matters._

---

## 1. Current Aspose parsing overview

### Where Aspose is imported/initialised

- `src/config.py:75` — `License()` applied at startup (best-effort; falls back to eval mode silently)
- `src/parsing/workbook_parser.py:17` — `from aspose.cells import Workbook`
- `src/parsing/cell_analyzer.py:7` — `from aspose.cells import Cell, CellValueType`

Only these three places. Aspose is **read-only** in the codebase — `wb.save()` is never called.

### Workbook loading

- `src/parsing/workbook_parser.py:962` — `wb = Workbook(str(file_path))`. Single line. No special options. Wrapped in try/except — fails noisily.

### Workbook-level metadata extraction

- `_extract_document_properties(wb)` (`workbook_parser.py:608`) — reads `wb.built_in_document_properties` for title, author, company, subject, keywords, comments, last_modified_by, created_date, modified_date
- `_detect_vba(file_path)` (`workbook_parser.py:48`) — zip-archive peek for `vbaProject.bin` (doesn't load macro bytes)
- Named ranges via `wb.worksheets.get_named_ranges()` at `workbook_parser.py:983`

### Sheet iteration

- `for sheet_idx, ws in enumerate(wb.worksheets):` at `workbook_parser.py:1004`
- Each iteration creates a `ParsedSheet(name, index, is_hidden)` object
- Hidden check: `ws.is_visible` (negated)

### Cell reading

- `for cell in ws.cells:` — sparse iteration, only non-empty cells
- `analyze_cell(cell, row_1based, col_1based)` in `src/parsing/cell_analyzer.py:175` returns a `CellInfo` Pydantic model
- 1-indexed row/col throughout the codebase (Aspose is 0-indexed; we convert at the boundary)

### Formula reading

- `cell.is_formula` boolean → triggers formula branch
- `cell.formula` string (`=SUM(C6:C7)` style)
- `cell.value` is the cached result
- Cell type derived from `cell.type` (Aspose `CellValueType` enum) mapped to our `CellType` enum in `_detect_cell_type` (`cell_analyzer.py:120`)

### Style/fill/locked status reading

- `cell.get_style()` returns a `Style` object (this is a **COPY** — modifications need explicit `set_style()`)
- Style fields extracted in `_extract_style` (`cell_analyzer.py:62`):
  - `font.is_bold`, `font.is_italic`, `font.is_strikeout` (note: `is_` prefix in Aspose Python)
  - `s.is_locked` (default `True`; meaningful only on protected sheets)
  - `s.indent_level` (int 0–15)
  - `s.rotation_angle` (float degrees)
  - `s.foreground_color` (fill) and `font.color` via `_argb_hex` helper (`cell_analyzer.py:44`) — strips alpha, returns hex
  - `s.custom` number format string (treats `"General"` as `None`)
  - `s.borders` — checks all 4 sides for non-NONE `line_style`
  - `s.horizontal_alignment`, `s.vertical_alignment` via `str().rsplit(".",1)[-1].lower()`

### Named ranges

- `_extract_named_ranges` via `wb.worksheets.get_named_ranges()` at `workbook_parser.py:983`
- Stored in `result.named_ranges` as `NamedRange(name, scope, destinations)` where `destinations = [nr.refers_to]`

### Data validations

- `_extract_validations(ws, sheet_name)` (`workbook_parser.py:198`)
- Iterates `ws.validations`
- For each: walks `v.areas` for cell ranges, reads `v.type` (int enum), `v.formula1`, `v.formula2`, `v.operator`, `v.ignore_blank`, `v.input_title`, `v.input_message`
- `_normalize_validation_type` (`workbook_parser.py:178`) maps int enum values 0–8 to canonical names (none, whole, decimal, list, date, time, textlength, custom, any) — Aspose stringifies these as digit strings, so the raw `str(v.type)` is `"3"` not `"list"`
- List-value resolution: `_resolve_validation_list_values` (`workbook_parser.py:828`) — for type=list, parses `formula1` as either a literal or a range reference (e.g. `Lookups!$A$2:$A$15`), walks parsed cells, returns actual allowed values. Runs as a second pass after all sheets parsed (`workbook_parser.py:1046`).

### Comments

- `_extract_comments(ws, sheet_name)` (`workbook_parser.py:140`)
- Iterates `ws.comments`, captures address, author, text (capped at 2000 chars)

### Text boxes / shapes

- `_extract_text_boxes(ws, sheet_name, cells)` (`workbook_parser.py:405`)
- Iterates `ws.shapes` (which includes text boxes, callouts, banners, etc.)
- Filters chart-shapes via `_shape_has_chart`
- Reads `.text` first, falls back to `.text_body` or `.html_text`
- Captures: name, text (≤2000 chars), `anchor_cell` (top-left), `coverage_range` (full bounding box like `"L4:O8"`), `shape_type` via `_shape_type_tag`, and `nearby_labels` (via `_find_nearby_labels` — primary search: col A/B/C labels at row range overlapping the box; secondary: any text within ±2 rows / ±8 cols)
- Bounding box from `shape.upper_left_row/column` AND `shape.lower_right_row/column`

### Hyperlinks

- `_extract_hyperlinks(ws, sheet_name)` (`workbook_parser.py:774`)
- Iterates `ws.hyperlinks`, reads `h.area`, `h.address`, `h.text_to_display`, `h.screen_tip`
- Classifies as internal if address contains `!` and doesn't start with `http://`, `https://`, `file:`, `mailto:` (split on first `!` → target sheet) [paste truncated here]

### Protected/hidden sheet handling

- **Hidden:** `ws.is_visible` negated → `ParsedSheet.is_hidden`. Hidden sheets ARE parsed (cells extracted), but downstream derivation passes (`mapping_builder.build_mapping`, `input_detector.build_input_schedule`, `build_business_logic_prompts`) skip them with `if sheet.is_hidden: continue`.
- **Protected:** `ws.protection.is_protected_with_password` OR `ws.is_protected` → `ParsedSheet.is_protected`. Used in `input_detector` to decide [meaningful] signal.

### Formula precedents/dependents

- Per formula cell: `cell.get_precedents()` returns Aspose `ReferredArea[]`
- `_expand_precedents` (`workbook_parser.py:124`) expands each area to qualified `Sheet!A1` strings (capped at `MAX_PRECEDENT_REFS_PER_CELL = 50`)
- Stored on `CellInfo.precedents`
- Aggregated workbook-wide into `FormulaGraph` via `build_formula_graph_from_precedents` (`src/parsing/formula_mapper.py:51`): links, `input_cells: list[str]` (referenced but not formulas), `output_cells: set[str]` (formulas not referenced)
- No string-parsing of formulas; everything comes from Aspose's dependency engine

### Merged cells

- `ws.cells.merged_cells` iterated at `workbook_parser.py:1019`. Captured as `MergedRange(range, min_row, min_col, max_row, max_col, value)` (value from the top-left cell)

### Row/column metadata

- **Row heights:** NOT extracted
- **Column widths:** only `_extract_narrow_columns` (`workbook_parser.py:761`) iterates and flags columns narrower than 3 chars (`< _NARROW_COL_THRESHOLD = 3.0`) as visual spacers
- **Frozen panes:** `_extract_frozen_panes` (`workbook_parser.py:684`) — tries `pane_state == 2` (FROZEN) + `get_panes().first_visible_row_of_bottom[…]` attribute name candidates; eval-mode unreliable
- **Print area:** `ws.page_setup.print_area`
- **Tab color:** `_extract_tab_color` via `ws.tab_color.to_argb()` → 6-char RGB hex
- **Page headers/footers:** `_extract_headers_footers` via `ps.get_header(0|1|2)` / `ps.get_footer(0|1|2)`, format codes (`&L`, `&C`, `&"font,style"`) [`format_codes`]

---

## 2. Current workbook hierarchy

It is **flat, not hierarchical**. That is the central architectural debt.

| Level | Object name | Source file | Key fields | Created | Persisted | Sent to LLM | In API |
|---|---|---|---|---|---|---|---|
| Workbook | `ParsedWorkbook` | `workbook_parser.py:106` | metadata, sheets[], named_ranges[], formula_graph | `parse_workbook()` | In-memory only; dies on restart | Yes (via builder → blueprint → prompts) | Via `/api/result/{job_id}` |
| Sheet | `ParsedSheet` | `workbook_parser.py:64` | cells[], merged_ranges[], regions[], comments[], data_validations[], text_box_notes[], hyperlinks[], tab_color, is_protected, frozen_rows/cols, print_area, used_max_[row/col] | Per-sheet loop in `parse_workbook` | In-memory only | Yes | Indirectly |
| Region (rectangle) | `DetectedRegion` | `schema.py` | cell_range, min/max_row/col, region_type (header_block / data_table […]) | `detect_regions()` in `region_detector.py` | In-memory only | Yes | Via blueprint |
| Section (classified) | `SectionClassification` | `schema.py` | section_type enum, confidence, source (local/llm), title, cell_range | `fallback.classify_region` (keyword) + LLM overlay | In-memory only | Yes | Via blueprint |
| Row-level field | `ExtractedField` | `schema.py` | sheet_name, row, label_text, data_cols, unit, is_formula, is_bold, [indent…], named_range, section_context | `extract_fields()` in `field_extractor.py` | In-memory only | Yes | Via blueprint |
| Cell | `CellInfo` | `schema.py` | address, row, col, value, cached_value, formula, cell_type, role, [style…] | `analyze_cell()` per Aspose cell | In-memory only | Indirectly via grids | Via blueprint |
| Metric candidate | `MetricCandidate` | `schema.py` | canonical_metric, metric_label, cell_address (label cell), data_range, metric_type, value_role, unit, confidence, source, evidence[] | `metric_classifier.classify_fields` | In-memory only | Yes | Via blueprint |
| Input field | `TemplateInputField` | `schema.py:332` | target cell, input_columns, current_period_col, needs_collection, [is_unlocked…], indent_level, named_range | `input_detector.build_input_schedule` | In-memory only | Yes (heavily) | Via blueprint |
| Author rule | `AuthorRule` | `schema.py:413` | source_type, source_location, raw_text, rule_category, summary, is_strict, affects_sheets/cells | LLM-extracted by Pass 1 in `deep_analyzer._run_template_logic` | In-memory only | Yes (TIER 0) | Via blueprint |
| Validation rule | `DataValidationRule` | `schema.py` | cell_range, validation_type, formula1/2, allowed_values[] | `_extract_validations` + `_resolve_validation_list_values` | In-memory only | Yes | Via blueprint |

**What is missing:**

- No explicit Section-of-rows model linking section title → metric rows → input fields
- No `MetricGroup` (parent rollup row with child sub-rows by indent)
- No `TemplateContract` — the blueprint serves both as raw extraction and as the to-be-approved artifact, which conflates two different jobs
- No database mapping for ANY of these models — they're all transient

---

## 3. Existing Pydantic models

All in `src/blueprint/schema.py`. None are mapped to database tables. All are transient.

| Model | Line | Purpose | Key fields |
|---|---|---|---|
| `CellInfo` | ~75 | Per-cell parsed data | address, row, col, value, cached_value, formula, cell_type, role, style: CellStyle, precedents: list[str] |
| `CellStyle` | ~62 | Visual + protection metadata | bold, italic, strikeout, font_size, font_color, fill_color, number_format, h_alignment, v_alignment, has_border, is_locked, indent_level, rotation_angle |
| `CellComment` | | Author note on a cell | cell_address, sheet_name, author, text |
| `DataValidationRule` | ~100 | Excel validation, with resolved list values | cell_range, validation_type, formula1/2, [allowed_]values: list[str] |
| `ConditionalFormatRule` | | Conditional formatting | cell_range, rule_type, operator, formula |
| `MergedRange` | | Merged cells | range, min/max_row/col, value |
| `NamedRange` | ~135 | Workbook-named cell or range | name, scope, destinations[] |
| `Hyperlink` | ~140 | Cell hyperlink | sheet_name, cell_address, display_text, url, [target_cell], tooltip |
| `TextBoxNote` | | Off-grid text with bbox + nearby labels | sheet_name, text, anchor_cell, coverage_range, nearby_labels: list[str], shape_type |
| `PageHeaderFooter`, `ChartCaption`, `PictureNote` | | Other off-grid content | various |
| `DetectedRegion` | | Contiguous rectangular block | cell_range, region_type, counts |
| `SectionClassification` | | Keyword/LLM-classified section | section_type: SectionType enum, confidence, source, title, cell_range, key_metrics[] |
| `ExtractedField` | | Row-level label+data extraction | sheet_name, row, label_text, data_cols[], unit, is_formula, indent_level, is_strikethrough, named_range, section_context |
| `MetricCandidate` | | Canonical metric match | canonical_metric, metric_label, cell_address, data_range, metric_type, value_role, unit, confidence, evidence[] |
| `FormulaInterpretation` | | Semantic tag for one formula | cell_address, formula, tag: FormulaSemanticTag, operands[], precedent_cells[] |
| `TemplateInputField` | ~332 | The user-fillable row spec | target_cell (current period), input_columns[], current_period_col/label, needs_collection, is_unlocked, downstream_cells[], dependent_formulas[], indent_level, named_range, canonical_metric |
| `InputSchedule` | | Aggregated input fields | total_input_fields, fields_needing_collection, [input_fields]: list[TemplateInputField], periods: list[DetectedPeriod] |
| `DetectedPeriod` | | One period column | col, label, parsed_date, period_type, status (current/future/budget/ytd/ltm) |
| `AuthorRule` | ~413 | LLM-extracted author-typed rule | rule_id, source_type, source_location, raw_text, [affects_]sheets/cells, is_strict, confidence |
| `BusinessRule` | | Pass 2 inferred business rule | rule_id, rule_type, description, threshold, applies_to, confidence |
| `ImpactChain`, `ImpactStep` | | Pass 2 metric flows | chain_id, name, steps[], business_significance |
| `FormulaAssessment`, `ConsistencyFinding` | | Pass 2 outputs | various |
| `DeepAnalysis` | ~450 | All Pass 1 + Pass 2 outputs | template_purpose, sheet_roles, data_flow_narrative, [formula_]assessments[], business_rules[], impact_chains[], consistency_findings[], sheet_business_context: dict |
| `SheetBlueprint` | | Per-sheet aggregated blueprint | name, is_protected, tab_color, frozen_rows/cols, [max_]col, was_truncated, row_labels: dict, column_headers: dict, regions[], sections[], section_titles[], time_structure, formula/input counts |
| `WorkbookMetadata` | | Top-level workbook props | filename, file_size_bytes, sheet_count, hidden_sheet_count, total_cells, total_formulas, total_named_ranges, has_vba, title, author, company, subject, comments, last_modified_by, created/modified_date |
| `TemplateBlueprint` | ~626 | The flat top-level dump | Everything above + template_purpose, template_pattern, cross_sheet_relationships, sheets[], template_metric_candidates[], extracted_fields[], input_schedule, deep_analysis, comments[], data_validations[], text_box_notes[], hyperlinks[], named_ranges[], etc. |

There is **no model called `TemplateContract`**. `TemplateBlueprint` is being used for that role and is the wrong shape.

---

## 4. Step-by-step parsing pipeline

### Step 1: Workbook load
- **Input:** Path to xlsx file
- **Process:** `wb = Workbook(str(file_path))`
- **Output:** Aspose Workbook object
- **Files:** `workbook_parser.py:962`
- **Testable:** file size matches expected; no exception thrown

### Step 2: Workbook-level metadata
- **Input:** Aspose Workbook
- **Process:** `_detect_vba` (zipfile peek), `_extract_document_properties`, `wb.worksheets.get_named_ranges()`
- **Output:** `WorkbookMetadata` populated (title, author, etc.); `result.named_ranges` populated
- **Files:** `workbook_parser.py:973-995`
- **Testable:** doc props match Excel File > Properties; named range count matches

### Step 3: Per-sheet iteration setup
- **Input:** Aspose Workbook
- **Process:** `for sheet_idx, ws in enumerate(wb.worksheets)` → instantiate `ParsedSheet`; capture `is_hidden = not ws.is_visible`; determine used range via `ws.cells.max_data_row/max_data_column`
- **Output:** `ParsedSheet` per sheet with bounds set
- **Files:** `workbook_parser.py:1004-1018`
- **Testable:** sheet count matches; used_max_row/col matches Excel's Ctrl+End

### Step 4: Merged cells per sheet
- **Input:** `ws.cells.merged_cells`
- **Process:** iterate, capture `MergedRange(range, min_row, min_col, max_row, max_col, value)` from top-left cell value
- **Output:** `parsed_sheet.merged_ranges[]`
- **Files:** `workbook_parser.py:1019-1041`
- **Testable:** merged-cell count matches Excel's visible merges

### Step 5: Cell-by-cell extraction
- **Input:** `ws.cells` (sparse iterator)
- **Process:** for each non-empty cell, call `analyze_cell(cell, row+1, col+1)` → `CellInfo`; collect formulas + precedents via `cell.get_precedents()` + `_expand_precedents`
- **Output:** `parsed_sheet.cells: list[CellInfo]`; `all_formula_cells` + `precedents_by_cell` maps for the graph builder
- **Files:** `workbook_parser.py:1042-1083`, `cell_analyzer.py:175`
- **Testable:** cell count matches expected; bold/locked/strikeout flags match a known fixture; formulas roundtrip

### Step 6: Regions + per-sheet structured extraction
- **Input:** `parsed_sheet.cells`
- **Process:** `detect_regions(cells, sheet_name)` (contiguous rectangles, gap-tolerant)
- **Output:** `parsed_sheet.regions: list[DetectedRegion]`
- **Files:** `region_detector.py:detect_regions`
- **Testable:** region count + cell ranges match visual inspection

### Step 7: Off-grid + per-sheet metadata extraction
- **Input:** Aspose worksheet
- **Process:** call `_extract_comments`, `_extract_validations`, `_extract_conditional_formats`, `_extract_text_boxes`, `_extract_headers_footers`, `_extract_chart_captions`, `_extract_pictures`, `_extract_hyperlinks`, `_extract_tab_color`, `_extract_frozen_panes`, `_extract_print_area`, `_extract_narrow_columns`; check `ws.is_protected`
- **Output:** all the per-sheet collections on `ParsedSheet`
- **Files:** `workbook_parser.py:1086-1107`
- **Testable:** a fixture with one of each surfaces correctly

### Step 8: Formula dependency graph (whole-workbook)
- **Input:** `precedents_by_cell` + `all_formula_cells`
- **Process:** `build_formula_graph_from_precedents` (`formula_mapper.py:51`) — folds per-cell precedents into a `FormulaGraph(links, input_cells, output_cells)`
- **Output:** `parsed.formula_graph`
- **Files:** `formula_mapper.py:51`
- **Testable:** input/output cell sets are correct on a small fixture (a simple `=SUM(A1:A3)` has 3 input cells and 1 output cell)

### Step 9: Second-pass list-validation resolution
- **Input:** `parsed.sheets` (cross-sheet cell access needed)
- **Process:** for each type=list validation, `_resolve_validation_list_values(v.formula1, sheet_cells_by_name)` — either splits literal `"A,B,C"` or walks the referenced range
- **Output:** `validation.allowed_values` populated
- **Files:** `workbook_parser.py:828`, `1046-1053`
- **Testable:** a fixture with both literal and range-based list validations

### Step 10: Section classification (deterministic)
- **Input:** regions + cells
- **Process:** `fallback.classify_region` keyword-matches region cell content against `_SECTION_KEYWORDS` (income statement, balance sheet, etc.)
- **Output:** `SectionClassification[]` per region
- **Files:** `intelligence/fallback.py:classify_region`
- **Testable:** a PL fixture's regions classify as `INCOME_STATEMENT` with confidence > 0.3

### Step 11: Section title finding (author-written headers)
- **Input:** cells + regions
- **Process:** `find_section_titles` (`region_detector.py`) — scans col A/B/C for bold/large/all-caps text on standalone rows
- **Output:** `SheetBlueprint.section_titles: list[str]`
- **Files:** `region_detector.py:find_section_titles`
- **Testable:** a fixture with "INCOME STATEMENT" bold in A1 returns it

### Step 12: Field extraction (row-level)
- **Input:** cells + regions + sections
- **Process:** `extract_fields` iterates rows with labels in col A/B/C and data in subsequent cols; captures label text, data_cols, sample_value, number_format, is_formula, is_bold, indent_level, is_strikethrough, unit (from number_format and label text), section_context (from section_by_row)
- **Output:** `ExtractedField[]` per sheet
- **Files:** `field_extractor.py:24`
- **Testable:** a row with "Revenue" in A5 and 1000/1100/1200 in C5:E5 produces an `ExtractedField` with `data_cols=[3,4,5]`

### Step 13: Metric classification
- **Input:** extracted fields
- **Process:** `classify_fields` (`metric_classifier.py`) matches `field.label_text.lower()` against 50+ `_METRIC_PATTERNS` (revenue, ebitda, leverage, etc.) → `MetricCandidate` with canonical name, type, evidence, confidence
- **Output:** `MetricCandidate[]`
- **Files:** `metric_classifier.py:classify_fields`
- **Testable:** "Total Revenue" classifies as `canonical="revenue"` with confidence ≥ 0.9

### Step 14: Named range cross-reference
- **Input:** extracted fields + named ranges
- **Process:** `_attach_named_ranges_to_fields` (`mapping_builder.py`) — for each named range, find fields whose row/col overlaps the range, attach the name
- **Output:** `ExtractedField.named_range` populated
- **Files:** `mapping_builder.py:129`
- **Testable:** a field at row 8 with named range `Revenue_May26` → `PL!E8` gets `named_range="Revenue_May26"`

### Step 15: Formula semantic tagging
- **Input:** formula cells + global cell lookup
- **Process:** `interpret_formulas` (`formula_semantics.py`) — tags formulas as margin/leverage/coverage/net_value/growth/sum_total/etc.
- **Output:** `FormulaInterpretation[]`
- **Files:** `formula_semantics.py:interpret_formulas`
- **Testable:** `=Debt/EBITDA` tags as `LEVERAGE`

### Step 16: Temporal analysis
- **Input:** cells (per sheet)
- **Process:** `detect_periods` (`temporal_analyzer.py`) — scans rows 1-12 for date headers ("Jan-26", "Q1 2026"), uses Actual/Forecast column markers, falls back to "rightmost historical = current" when no exact match
- **Output:** `DetectedPeriod[]` per sheet
- **Files:** `temporal_analyzer.py:detect_periods`
- **Testable:** a fixture with "Actual" markers C-E and "Forecast" F produces current_period at column E

### Step 17: Input field detection
- **Input:** extracted fields, candidates, periods, formula graph, per-sheet is_protected
- **Process:** `build_input_schedule` (`input_detector.py`) — six signals: (0) `is_locked=False` on protected sheet (authoritative), (1) in formula graph input set, (2) has downstream dependents, (3) input-style fill color, (4) non-formula in period column, (5) mixed-row pattern. Plus downstream BFS via `_bfs_downstream` (3 hops, max 20 cells)
- **Output:** `TemplateInputField[]` aggregated into `InputSchedule`
- **Files:** `input_detector.py:66`
- **Testable:** a protected sheet with E6/E7 unlocked produces 2 input fields with `is_unlocked=True`

### Step 18: Blueprint assembly
- **Input:** `ParsedWorkbook` + `MappingResult` (fields + candidates + interpretations + ambiguities + zones + input schedule)
- **Process:** `build_blueprint` (`builder.py`) — wraps everything into a `TemplateBlueprint`; computes row_labels and column_headers per sheet; [aggregates regions]/text boxes etc. workbook-wide
- **Output:** `TemplateBlueprint`
- **Files:** `blueprint/builder.py:build_blueprint`
- **Testable:** blueprint has expected counts for sheets/fields/candidates/inputs

### Step 19: LLM Pass 1 (Template Logic)
- **Input:** parsed workbook + mapping
- **Process:** `build_template_logic_prompts` → Claude call (Sonnet 4.6, max 6144 tokens) → parse JSON for template_purpose, sheet_roles, [data_]decisions, author_rules[]
- **Output:** populated `DeepAnalysis` Pass-1 fields
- **Files:** `deep_prompts.py:build_template_logic_prompts`, `deep_analyzer.py:_run_template_logic`
- **Testable:** a workbook with one text box rule produces an `AuthorRule` with that text verbatim

### Step 20: LLM Pass 2 (Business Logic)
- **Input:** parsed + mapping + Pass 1 result
- **Process:** `build_business_logic_prompts` → Claude call (max 8192 tokens) → parse JSON for sheet_business_context, formula_assessments, business_rules, impact_chains, consistency_findings
- **Output:** populated `DeepAnalysis` Pass-2 fields
- **Files:** `deep_prompts.py:build_business_logic_prompts`, `deep_analyzer.py:_run_business_logic`
- **Testable:** a workbook with a covenant formula produces a `BusinessRule` of type `covenant_threshold`

**End state:** a `TemplateBlueprint` containing everything. No `TemplateContract`. No persistence.

---

## 5. What to rebuild first in the new codebase

### Build Step 1: Workbook upload and raw file loading
- **Goal:** store the uploaded xlsx durably and load it with Aspose
- **Implement:** `POST /api/v1/template/upload` → save file to filesystem or Supabase Storage; create a `Template` row + `TemplateFile` row in DB; return `template_id`
- **Models/tables:** `Template`, `TemplateFile`
- **Test:** upload a sample xlsx; confirm row in DB; confirm file readable

### Build Step 2: Workbook metadata and sheet list
- **Implement:** `parse_workbook_metadata(file_path) -> WorkbookMetadata`; iterate sheets; persist `TemplateSheet` rows
- **Models/tables:** `TemplateSheet` (name, index, is_hidden, is_protected, tab_color, used_max_row, used_max_col)
- **Test:** 16-sheet fixture → 16 rows with correct metadata

### Build Step 3: Used-range respect + cell extraction
- **Implement:** port `analyze_cell` from `cell_analyzer.py` verbatim. Iterate `ws.cells` per sheet, store cells per sheet (memory only at this stage — persist row labels + col headers only)
- **Models/tables:** `TemplateSheetCell` is optional (huge volume). Recommended: store derived row labels and column headers as JSON columns on `TemplateSheet`, NOT every cell
- **Test:** a small fixture's row labels and column headers persist correctly

### Build Step 4: Styles + locked-cell extraction
- **Implement:** port `_extract_style` verbatim including the strikeout fallback and indent/rotation reads
- **Models/tables:** stored as JSON on cell records or derived field records
- **Test:** a fixture with one unlocked cell on a protected sheet correctly flags it

### Build Step 5: Named ranges
- **Implement:** port `wb.worksheets.get_named_ranges()` extraction
- **Models/tables:** `TemplateNamedRange(name, scope, refers_to)`
- **Test:** 23 named ranges in the Flash Collection sample → 23 rows

### Build Step 6: Data validations + list resolution
- **Implement:** port `_extract_validations` + `_normalize_validation_type` + `_resolve_validation_list_values`. The list-resolution must run as a second pass.
- **Models/tables:** `TemplateValidation` with `allowed_values` JSON column
- **Test:** a fixture with literal list `"Air,Sea,Land,Rail"` and a cross-sheet range list both resolve to actual values

### Build Step 7: Comments and text boxes
- **Implement:** port `_extract_comments` and `_extract_text_boxes` (with bounding box + nearby_labels — both required for Tier 0 quality)
- **Models/tables:** `TemplateComment`, `TemplateTextBox(text, anchor_cell, coverage_range, nearby_labels JSON)`
- **Test:** a fixture with text box at L4:O8 next to Revenue/COGS rows → nearby_labels contains them

### Build Step 8: Hyperlinks
- **Implement:** port `_extract_hyperlinks` with internal/external classification
- **Models/tables:** `TemplateHyperlink`
- **Test:** internal `Sheet!A1` and external `https://...` correctly classified

### Build Step 9: Formula dependency graph
- **Implement:** port `cell.get_precedents()` + `_expand_precedents` + `build_formula_graph_from_precedents`. Persist the edges.
- **Models/tables:** `TemplateFormulaEdge(source_cell, target_cell, formula_text)`
- **Test:** a fixture `=SUM(A1:A3)` produces 3 edges, all targeting the SUM cell

### Build Step 10: Raw workbook graph snapshot
- **Implement:** snapshot endpoint that returns all of the above as a single JSON payload (debug/inspection)
- **Models/tables:** none new
- **Test:** payload JSON-validates and is ≤ 1 MB for medium templates

### Build Step 11: Detect sheet roles
- **Implement:** code-first heuristic (tab color, name pattern, hidden/protected) → sheet_role guess. LLM only for ambiguous cases. Persist on `TemplateSheet.role` and `TemplateSheet.role_confidence`
- **Test:** a sheet named "Developer" with red tab → role="internal" with high confidence

### Build Step 12: Detect sections
- **Implement:** port `detect_regions` + `find_section_titles` + `classify_region` (deterministic keyword). LLM upgrade only on second pass.
- **Models/tables:** `TemplateSection(sheet_id, title, cell_range, region_type, classification, classification_source)`
- **Test:** 5 sections on a PL fixture; titles match author-written headers when present

### Build Step 13: Detect period columns
- **Implement:** port `temporal_analyzer.detect_periods` including the Actual/Forecast boundary fallback
- **Models/tables:** `TemplatePeriod(sheet_id, col, label, parsed_date, period_type, status)`
- **Test:** KPI fixture with mixed monthly columns produces correct current_period

### Build Step 14: Detect metric rows
- **Implement:** port `extract_fields` and `classify_fields`. Persist as `TemplateMetricRow` rows
- **Models/tables:** `TemplateMetricRow(sheet_id, section_id?, row, label_text, label_cell, canonical_metric, metric_type, unit, indent_level, [parent_metric_row_id?], [data_range_id?], is_strikethrough)` — note the parent linkage for hierarchy
- **Test:** Revenue / Product Revenue (indent 1) / Service Revenue (indent 1) / Total Revenue produces correct `parent_metric_row_id` linkage

### Build Step 15: Detect input fields
- **Implement:** port `input_detector.build_input_schedule` with cell-locking primary signal
- **Models/tables:** `TemplateField(metric_row_id, target_cell, input_columns JSON, current_period_col, current_period_label, is_unlocked, needs_collection, downstream_cells JSON, named_range_id?)`
- **Test:** a protected sheet with unlocked cells correctly identifies inputs; non-protected sheet falls back to heuristics

### Build Step 16: Extract author rules (LLM with code constraints)
- **Implement:** port Pass 1 prompt + extraction. Add code-side validation: `rule_category` must be in a `Literal[...]` taxonomy; reject categories Claude invents
- **Models/tables:** `TemplateAuthorRule(rule_category, source_type, source_location, raw_text, summary, is_strict, affects JSON, confidence)`
- **Test:** a fixture with a sign-convention text box produces an `AuthorRule` with `category=sign_convention`

### Build Step 17: Compile policies (the missing layer)
- **Implement:** a `PolicyRegistry` with decorator-based registration. For each `AuthorRule`, attempt to bind to an executable: `validate_sign`, `validate_in_allowed_list`, `validate_balance_check`. LLM proposes binding + args; code validates the function exists and args are compatible
- **Models/tables:** `TemplatePolicy(author_rule_id, policy_function, policy_args JSON, binding_confidence)`
- **Test:** a sign-convention rule binds to `validate_sign(expected_sign="positive")`

### Build Step 18: Compile draft Template Contract
- **Implement:** aggregate everything above into a `TemplateContract` row. This is a snapshot of "this is the template, ready for human review"
- **Models/tables:** `TemplateContract(template_version_id, status="draft", compiled_at, summary JSON, identity JSON)`
- **Test:** end-to-end on Flash Collection produces a contract row with sensible identity (PE_SPONSOR_MONTHLY archetype, GBP unit, monthly frequency)

### Build Step 19: Human review + approve
- **Implement:** `GET /api/v1/contract/{id}` (view + edit), `POST /api/v1/contract/{id}/approve`. Approval bumps `status="approved"`, freezes a new version
- **Test:** approve → re-fetch shows `status=approved` + immutability

### Build Step 20: MVP done
Stop here. The contract is the artifact Stage 2 will bind to.

---

## 6. What existing logic is worth reusing

| Component | File | Why useful | Maturity | Action |
|---|---|---|---|---|
| Aspose extraction (cells + styles + precedents) | `parsing/cell_analyzer.py`, `parsing/workbook_parser.py` | Took weeks to de-risk (is_strikeout naming, validation int enums, shape text_body fallback, freeze pane reading, etc.) | High | Copy directly — vendor it into the new repo |
| `_resolve_validation_list_values` | `workbook_parser.py:828` | Solves the list[-value] resolution cleanly | High | Copy directly |
| `_extract_text_boxes` with bbox + nearby_labels | `workbook_parser.py:405` + `_find_nearby_labels` | Section-adjacent annotations are a Tier-0 input | High | Copy directly |
| `temporal_analyzer.detect_periods` (post-rewrite) | `parsing/temporal_analyzer.py` | Actual/Forecast boundary detection + rightmost-historical fallback | Medium | Copy, simplify (drop FY/quarter regex if not needed for MVP) |
| `input_detector.build_input_schedule` (signal stack) | `parsing/input_detector.py` | Cell-locking [primary signal] base pattern | High | Copy, but persist results instead of just returning |
| `formula_mapper.build_formula_graph_from_precedents` | `parsing/formula_mapper.py` | Aspose precedent → graph, no string parsing | High | Copy directly |
| Pass 1 author-rule extraction prompt | `intelligence/deep_prompts.py:build_template_logic_prompts` | Well-tuned JSON schema with verbatim raw_text rule | Medium | Copy the prompt; rewrite the parsing side to validate against a closed taxonomy |
| `field_extractor.extract_fields` | `parsing/field_extractor.py` | Indent-level / [label] position logic | Medium | Copy, but feed into hierarchical `TemplateMetricRow` with parent linkage |
| `region_detector.find_section_titles` | `parsing/region_detector.py` | Author-written titles, not just classification | Medium | Copy directly |
| Document properties + tab color + frozen panes extractors | various in `workbook_parser.py` | Eval-mode defensive coding learned the hard way | High | Copy directly |
| Smoke test patterns | `scripts/smoketest_tier1_3.py` | Synthetic xlsx with `_commit_style` helper for Aspose's copy-on-read style gotcha | Medium | Adapt as the new test fixture pattern |

---

## 7. What existing logic should be discarded or redesigned

| Issue | Current code | What to do |
|---|---|---|
| Prompt context too large — 23K per chat turn, 94K for Pass 2 | `chat.py:_build_context`, `deep_prompts.py:build_business_logic_prompts` | Don't carry forward the chat at all. Pass 1 prompt can be reused; Pass 2 needs redesign — the 60×30 grid is brute-force token waste |
| Data model is flat | `schema.py:TemplateBlueprint` (~50 sibling fields) | Build hierarchy via DB foreign keys: Workbook → Sheet → Section → MetricRow → Field. Use JSON columns sparingly |
| Sheet/section/metric hierarchy missing | n/a | Step 14 above — `TemplateMetricRow.parent_metric_row_id` is the linchpin |
| Author rules are prompt text only | `schema.py:AuthorRule`, no executable layer | Build `PolicyRegistry` with code-callable functions; `AuthorRule` becomes evidence, `Policy` becomes executable |
| Raw extraction mixed with business interpretation | `TemplateBlueprint` mixes Aspose-extracted cells with Pass 1 template_purpose / business_rules | Split into separate layers and separate tables: `template_*` for extraction, `template_contract_*` for interpretation |
| No persistence | `_jobs: dict` in `routes.py:33` | SQLite via SQLAlchemy from day 1 |
| Chat-oriented | `chat.py`, `static/` SPA, suggestion buttons | Replace with a review/approve UI. Chat optional later |
| Pass 2 brute-force grids | `deep_prompts.py:build_business_logic_prompts` | Replace with targeted per-section LLM calls driven by hierarchical model |
| Dead code | `intelligence/analyzer.py`, `intelligence/prompts.py`, `intelligence/shortcut_client.py` | Delete during port |
| `TemplateBlueprint` as do-everything object | `schema.py:626` | Split into `WorkbookSnapshot` (raw extraction), `TemplateAnalysis` (LLM outputs), `TemplateContract` (approved artifact) |
| In-memory JobState polling | `routes.py:33`, status endpoints | Replace with `AnalysisJob` DB rows + status-by-id endpoint |
| Frontend cache-buster hacks (v=6) | `static/index.html` | Forget the SPA. Server-rendered review page or a proper React frontend later |

---

## 8. Recommended new architecture for the parser

Four physical layers, each with its own module and tables.

### Layer 1: Raw Excel extraction (`raw_extraction/`)
- Pure Aspose calls. No interpretation. No LLM.
- **Modules:** `loader.py`, `cell_extractor.py`, `metadata_extractor.py`, `validation_extractor.py`, `shape_extractor.py`, `hyperlink_extractor.py`, `formula_graph.py`
- **Output Pydantic schemas:** `RawWorkbook`, `RawSheet`, `RawCell`, `RawNamedRange`, `RawValidation`, `RawTextBox`, `RawHyperlink`, `RawFormulaEdge`
- **DB tables:** `template_files`, `template_sheets`, `template_named_ranges`, `template_validations`, `template_comments`, `template_text_boxes`, `template_[formula_edges]`
- **API:** `POST /api/v1/template/upload` → returns `template_id`; `GET /api/v1/template/{id}/raw` returns the snapshot JSON
- **Tests:** cell-count match, formula extraction roundtrip, list-validation resolution, internal-vs-external hyperlink classification, formula-edge graph integrity

### Layer 2: Structural detection (`structure/`)
- Code-first heuristics. LLM optional, deterministic primary.
- **Modules:** `region_detector.py`, `section_title_finder.py`, `field_extractor.py`, `temporal_analyzer.py`, `metric_classifier.py`, `input_detector.py`, `formula_semantics.py`
- **Output schemas:** `DetectedSection`, `DetectedPeriod`, `DetectedMetricRow`, `DetectedInputField`
- **DB tables:** `template_sections`, `template_periods`, `template_metric_rows` (with `parent_metric_row_id` for hierarchy), `template_fields`
- **API:** `POST /api/v1/template/{id}/analyze/structure` → kicks off analysis job; `GET /api/v1/template/{id}/structure` returns results
- **Tests:** parent linkage (Revenue → Product Revenue), period status correctness, cell-locking primary signal

### Layer 3: Business interpretation (`interpretation/`)
- LLM-driven, with code-side validation against closed taxonomies.
- **Modules:** `pass1_template_logic.py` (purpose, sheet roles, author rules), `pass2_business_logic.py` (formula meaning, business rules, impact chains), `taxonomy.py` (closed enums for rule categories)
- **Output schemas:** `TemplateIdentity`, `SheetRole`, `AuthorRule`, `BusinessRule`, `ImpactChain`
- **DB tables:** `template_author_rules`, `template_business_rules`, `template_impact_chains`, `template_analysis_jobs`
- **API:** `POST /api/v1/template/{id}/analyze/interpret` → kicks off Pass 1 + Pass 2; `GET /api/v1/template/{id}/interpretation`
- **Tests:** rule extraction from a known text-box fixture, category-validation rejecting Claude-invented categories

### Layer 4: Execution contract (`contract/`)
- The approved, versioned, executable artifact. Bound to policies that are real Python functions.
- **Modules:** `compile.py` (assembles the contract from layers 1-3), `policies/` (executable functions registered to a registry), `contract_model.py`
- **Output schemas:** `TemplateContract`, `InputContract`, `Policy{Evidence,Interpretation,Executable}`, `ContractIdentity`
- **DB tables:** `template_contracts` (with status: draft|approved), `template_contract_inputs`, `template_contract_policies`, `audit_events`
- **API:** `POST /api/v1/template/{id}/contract/compile`, `GET /api/v1/contract/{contract_id}`, `POST /api/v1/contract/{contract_id}/approve`
- **Tests:** contract end-to-end on a real fixture, policy binding sanity, approval immutability

---

## 9. Database schema (minimum MVP)

```sql
-- Identity
organizations(id, name, created_at)
users(id, org_id FK, email, name, role, created_at)

-- Templates
templates(id, org_id FK, name, sponsor_name, created_by FK→users, created_at)
template_versions(id, template_id FK, version_number, source_file_id FK, parsed_at, created_by FK)
template_files(id, template_version_id FK, storage_path, original_filename, sha256, size_bytes)

-- Raw extraction (Layer 1)
template_sheets(id, template_version_id FK, name, index, is_hidden, is_protected,
                tab_color, used_max_row, used_max_col, frozen_rows, frozen_cols,
                print_area, row_labels JSON, column_headers JSON)
template_named_ranges(id, template_version_id FK, name, scope, refers_to)
template_validations(id, template_sheet_id FK, cell_range, validation_type, formula1,
                     formula2, operator, prompt_message, allowed_values JSON)
template_comments(id, template_sheet_id FK, cell_address, author, text)
template_text_boxes(id, template_sheet_id FK, text, anchor_cell, coverage_range,
                    nearby_labels JSON, shape_type)
template_hyperlinks(id, template_sheet_id FK, cell_address, url, display_text,
                    is_internal, target_sheet, target_cell)
template_formula_edges(id, template_version_id FK, source_cell, target_cell, formula_text)

-- Structural detection (Layer 2)
template_sections(id, template_sheet_id FK, title, cell_range, region_type,
                  classification, classification_source, confidence)
template_periods(id, template_sheet_id FK, col, label, parsed_date, period_type, status)
template_metric_rows(id, template_sheet_id FK, section_id FK?, row,
                     label_text, label_cell, canonical_metric, metric_type, unit,
                     indent_level, parent_metric_row_id FK?,
                     named_range_id FK?, is_strikethrough, confidence)
template_fields(id, metric_row_id FK, target_cell, input_columns JSON,
                current_period_col, current_period_label, is_unlocked,
                needs_collection, downstream_cells JSON, named_range_id FK?,
                input_evidence JSON)

-- Interpretation (Layer 3)
template_author_rules(id, template_version_id FK, rule_category, source_type,
                      source_location, raw_text, summary, is_strict, affects JSON,
                      confidence, extracted_by, extracted_at)
template_business_rules(id, template_version_id FK, rule_type, description,
                        threshold, applies_to JSON, confidence)
template_context_notes(id, template_version_id FK, scope, note_type, content)
analysis_jobs(id, template_version_id FK, job_type, status, started_at, completed_at,
              error, llm_tokens_used)

-- Contract (Layer 4)
template_contracts(id, template_version_id FK, status, compiled_at,
                   approved_by FK?, approved_at, identity JSON, summary JSON)
template_policies(id, contract_id FK, author_rule_id FK?, policy_function,
                  policy_args JSON, binding_confidence)
template_contract_inputs(id, contract_id FK, target_cell, canonical_metric,
                         expected_type, expected_unit, expected_sign,
                         expected_scale, applied_policy_ids JSON)

-- Audit (Layer 4, design-now-pay-later)
audit_events(id, actor_user_id FK, event_type, target_type, target_id,
             before_json, after_json, occurred_at)
```

**Notes:**

- Use BIGINT id primary keys, `created_at`/`updated_at` on every table, soft-delete (`deleted_at`) only when you confirm a need.
- `template_versions` is what `template_*` tables hang off so you can re-parse a template without losing the old version. The `Template` row is the user-named template; `TemplateVersion` is each parse.
- Most JSON columns are short structured arrays (≤2KB); avoid storing megabytes of cell data in Postgres rows.
- Postgres-native (`jsonb`) for Supabase. SQLite has JSON1 if you start there.

---

## 10. Testing plan

| Test | Fixture | Expected output |
|---|---|---|
| Unit: cell type detection | A workbook with one of each: number, string, formula, date, boolean, error, empty | CellType enum matches |
| Unit: locked-cell extraction | A protected sheet with one explicitly unlocked cell | `is_locked=False` on that cell only |
| Unit: strikethrough capture | A cell with `font.is_strikeout=True` | `style.strikeout=True` |
| Unit: indent-level capture | Cells at indent 0, 1, 2 | indent levels captured correctly |
| Unit: validation list literal | A list validation with `formula1='"A,B,C"'` | `allowed_values=["A","B","C"]` |
| Unit: validation list range | A list validation referencing `Lookups!A2:A4` | `allowed_values` from the target cells |
| Unit: named range parse | A workbook named range pointing at `Sheet1!E8` | NamedRange with correct destination |
| Unit: formula precedents | `=SUM(A1:A3)` in B1 | 3 precedent entries: `Sheet!A1`, `Sheet!A2`, `Sheet!A3` |
| Unit: text box with bbox + nearby | A text box at L4:O8 next to A4..A7 row labels | `coverage_range="L4:O8"`, `nearby_labels` contains 4 entries |
| Unit: internal hyperlink classification | A link to `Sheet2!A1` | `is_internal=True`, `target_sheet="Sheet2"` |
| Unit: tab color | A sheet with red tab | red hex (or close) |
| Unit: indent hierarchy | Revenue (0) / Product Revenue (1) / Service Revenue (1) / Total Revenue (0) | links children to Revenue |
| Unit: current period detection — explicit Actual marker | Headers row 1: "Actual" C–E, "Forecast" F. Headers row 2: Mar/Apr/May/Jun. | current_period at column E |
| Unit: current period — fallback to rightmost historical | Headers Mar/Apr/May (all historical). Today = late June | May classified as current |
| Unit: input detector — unlocked-cell primary signal | Protected sheet, E6/E7 unlocked | Two TemplateField rows with `is_unlocked=True` |
| Unit: input detector — non-protected fallback | Same fixture without protection | falls back to heuristics; still detects inputs but `is_unlocked=False` |
| Integration: small fixture | Synthetic xlsx, 3 sheets, 30 cells | End-to-end produces a `TemplateContract` |
| Integration: real sponsor template | `Flash Collection_MasterTemplate_2.1.6_PROD.xlsx` | 16 sheets parsed, KPI's 49×119 used range respected, 23 named ranges, 33+ validations on PL, author rules extracted from text boxes |
| Snapshot: per-sheet structure JSON | Each fixture | committed `.snap.json` file (regenerate on intentional changes) |
| Snapshot: contract JSON | Each fixture | Same as above |
| Hidden-sheet handling | A workbook with one hidden sheet | Hidden sheet's cells extracted, but excluded from `template_fields` (downstream filter) |
| Protected-sheet handling | Mixed protected + unprotected sheets | `is_protected` correctly per sheet |
| Formula-dependency graph | Multi-step formulas A1 → B1 → C1 | Graph has correct edge chain; BFS from A1 returns B1 and C1 |
| Author rule extraction (LLM) | A text box with "Use £ millions throughout" | `AuthorRule` with `category=unit_convention`, raw_text verbatim |
| Author rule taxonomy enforcement | A Claude response with an unknown category like "weather_dependent" | Code rejects or normalizes to "other" |
| Policy binding (LLM proposes, code validates) | A sign-convention rule | binds to `validate_sign(expected_sign="positive")`; if Claude proposes `validate_made_up_func`, code rejects |
| Contract approval immutability | Approve, then attempt to mutate | mutation rejected; new contract version required for changes |

**Two fixture types you need from day 1:**

1. `tests/fixtures/synthetic/` — small handcrafted xlsx files (use aspose + the `_commit_style` helper pattern from the existing repo) — one per unit test
2. `tests/fixtures/real/` — the Flash Collection real template (acquire permission, anonymize if needed) — for integration tests

---

## 11. Final migration summary

- The existing parser currently reaches Stage 1C (target input field detection), partial 1D (author-rule extraction is LLM-only, no executable layer).
- The strongest reusable component is `src/parsing/` — specifically `cell_analyzer.py`, the Aspose helpers in `workbook_parser.py` (text boxes, validation list resolution, hyperlinks, frozen panes, tab colors, document properties), and `formula_mapper.build_formula_graph_from_precedents`. Treat the whole parsing folder as a vendored library; copy verbatim.
- The weakest/messiest part is the data model. `TemplateBlueprint` is a flat 50-field bag that mixes raw extraction with LLM-derived business interpretation. It is the wrong shape for a contract. The chat layer (`chat.py`, `static/`) and the dump-everything prompt architecture (`serializers.py`, `deep_prompts.py` Pass 2) are the second-weakest — discard outright.
- The first thing to rebuild is the **database schema and the layered package structure** — `raw_extraction/`, `structure/`, `interpretation/`, `contract/`. Stub all four with empty modules and run a no-op test through the whole stack BEFORE writing extraction code. This locks the architecture before any momentum builds in the wrong direction.
- The first thing to test is `parse_workbook_metadata` on a small fixture — proves Aspose binding works, file IO works, DB persistence works. Anything more ambitious as test #1 is over-scoping.
- **Do not rebuild yet:** the chat layer, the SPA, Pass 2 grid prompts, the `serializers.py` LLM-prompt helpers, the conditional-format rule extraction (low-signal), the `inspect_pipeline.py` debug tool. These come back (or don't) after Stage 1 MVP ships.
- **The clean MVP milestone is:** a PE admin user uploads a sponsor xlsx, the system extracts and structures it, runs Pass 1 + bound policies, [renders the] Contract JSON in a review UI, the user clicks approve, the contract is durably marked approved with an audit row. That's it. No source workbook. No mapping. No write proposals. No add-in. End there. Stage 2 is the next milestone — and it gets to start from a clean contract.
