# Population — Authoring & Refresh Plan

> Status: design / for discussion. Not yet built.

## Why
Population today is a **value-binding engine**: it maps source numbers into fixed,
pre-identified input cells. Two capabilities are missing, and they share a root —
the system models a template as a *static grid of cells*, not as a *living
document with rules*:

1. **Authoring** — adding custom metrics / new KPIs / new line items in the areas
   the template *allows* (not just filling cells that already exist).
2. **Refresh** — replacing a prior fill cleanly, so a template that already
   contains another company's data doesn't leak stale values into a new fill.

---

## Part 1 — Refresh: don't leave stale data

### The principle: blank master + clean fill
- A stored template should be a **blank master**: structure + formulas only, no
  company data. The other company's data currently in the file is contamination.
- Every population run produces a **fresh filled copy for one company** from
  *(blank master + source)*. The master never accumulates data.

### What to build
1. **Clear-then-fill** (small, high value): before writing, **clear every in-scope
   input cell** (`category` ∈ data/sourced), then fill the matched ones. Unmatched
   inputs end up **empty, not stale**. Formula/computed cells and structure are
   never touched (the formula-aware categorization already protects them).
2. **Normalize-to-blank** (optional): a one-time action to clear all input cells of
   an already-contaminated stored template, turning it into a clean master.

### DECISION (2026-06-15): no blank master available
A genuinely blank version of the template can't be provided, so we cannot do the
simple "fresh copy from blank." We must **reset the contaminated template safely**:

- **Clear = full reset of the fillable surface**, not just numbers:
  1. fixed input **values** (easy — from the data model), AND
  2. previous fillers' **added labels + values** in the extensible regions
     (so the new fill doesn't inherit the prior company's custom KPIs/fields).
- **This couples the two features**: clearing the added labels safely REQUIRES the
  extensible-region map (Part 2A) — you can't blindly delete label cells or you'd
  wipe permanent structure ("Revenue", totals, formulas). So region detection is a
  prerequisite for the *full* reset, not just for adding.
- **Risk control (no blank reference):** label-clearing is destructive and there's
  no blank master to diff against, so it runs **only inside confirmed extensible
  add-areas**, never on fixed labels/formulas/totals, and a **preview/confirm step**
  shows exactly what will be cleared before writing. (Fits the "human adds context"
  principle.)

### Still-open practical decisions
- **Partial source (e.g. only the current month): clear everything in scope, or
  only the periods/scenarios present in the source?** (Rolling-flash question,
  tied to the as-of date.)
- **Prior periods**: when filling March, do Jan/Feb come from the source, or
  persist? (Needs a call once the as-of/period model is settled.)

---

## Part 2 — Authoring: add custom metrics / KPIs / line items

### What's missing
No concept of an **extensible region** (a place the template invites additions) or
**authoring rules** (what's allowed where). So population can only fill existing
cells — it can't create a new KPI row with its label + values.

### A. Analysis — detect extensible regions
Add an `extensible_regions` output to understanding. Signals:
- blank repeating rows under a section with the same column shape as the filled
  rows above (a list with empty slots),
- "(specify)" / "Other…" / "Add KPI" style labels,
- a custom/open block the model recognises (KPI table, other-adjustments),
- data-validation dropdowns on label cells,
- a subtotal row whose SUM range already spans the blank rows.

Each region records: sheet, label column, value/period columns, available row
range + capacity, what each new line needs (label, values, unit, sign), the
block's total cell + its formula, the region kind, and any rules.

### B. Contract — store the authoring rules
Persist `extensible_regions` with the data model. This is the part of the original
**Template Contract** vision ("rules the PortCo must follow when filling it in")
that was never actually built.

### C. Population — an "add line item" action
Two-phase populate:
1. **Bind** (today): map known metrics → fixed input cells (formula-aware).
2. **Extend** (new): for source metrics/KPIs that map to NO fixed input but fit an
   extensible region, the matcher proposes a new line (region, label, per-period
   source cells). Deterministic apply then:
   - writes into the **next free blank rows** — **never inserts rows** (inserting
     into a ~90%-formula model risks breaking references),
   - writes label + values and copies a sibling row's formatting so it looks native,
   - **respects capacity** (overflow is reported, not forced),
   - **never touches the total row** (it already sums the range).

### Practical decisions (need your call)
- **How eager to add?** Conservative (only when the source clearly has it AND the
  region clearly invites it) vs aggressive. Recommend conservative + show proposals.
- **Blank rows only vs inserting rows?** Recommend blank-rows-only (safety).
- **Review step?** New line items are higher-risk than value fills — surface them
  for approval before writing.

---

## Phasing (given: no blank master)
1. **Clear input VALUES then fill** — small, no region detection needed. Kills the
   stale-*number* leak immediately. (Added labels/KPIs still linger until phase 3.)
2. **Detect + store extensible regions** (analysis + contract) — the foundation for
   both the full reset and adding.
3. **Full reset**: also clear added labels/values in extensible add-areas
   (preview/confirm; add-areas only). Now no stale fields survive.
4. **Add-line-item in population** (next free rows only, totals-safe, capacity-aware,
   review step).
5. **Authoring rules / validation** (units, %, allowed values).

Phases 1 ships value now; 2 unlocks 3 and 4.

## The unifying idea
Both features move the system from *"fill this grid"* to *"complete this workbook
the way a finance person would — fill what's fixed, add what's allowed, and don't
carry over anyone else's data."* That's the Template Contract working as intended.
