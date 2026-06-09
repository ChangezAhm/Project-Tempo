# Project Tempo — MVP Specification

> **Status:** MVP scope (week 1 build)
> **Last updated:** 2026-06-09

---

## 1. Vision

A system for **private equity firms** to upload, manage, and standardise their **sponsor reporting templates**, so that **PE-backed portfolio company finance teams** can populate those templates quickly, accurately, and without stress.

The product gives the PE firm **control, consistency, and confidence** in how their reporting templates are interpreted, while making sponsor reporting easier for the portfolio company finance teams who actually fill them in.

---

## 2. The Core Idea — the "Template Contract"

The product starts with the PE firm uploading a target Excel template, for example:

- Monthly flash report
- Covenant pack
- KPI dashboard
- Valuation input template
- Liquidity analysis
- Debt schedule
- Management accounts pack

The system analyses that template and turns it into a reusable **Template Contract**: a structured, reviewed map of:

- What the sponsor is asking the portfolio company to provide
- Where each data point should go
- What each section is for
- What business logic applies
- What rules the portfolio company must follow when filling it in

Once approved, the Template Contract becomes the **reusable source of truth** for that sponsor template.

---

## 3. MVP Scope

The first MVP is a **web app for template onboarding and contract creation**.

**Flow:**

1. A PE user uploads a sponsor template.
2. The backend parses it with **Aspose.Cells** and extracts the raw Excel truth.
3. The **LLM** interprets the business purpose of the workbook.
4. The system produces a **reviewable Template Contract** (a grid of detected fields and sections).
5. The PE user can **approve, correct, reject, or manually label** detected fields and sections.
6. The user can **add more context** to the template, which the system stores and calls upon later when populating for a portfolio company finance team.
7. Once approved, the Template Contract is saved as the reusable source of truth.

**The MVP stops at creating the trustworthy, reviewed Template Contract.** Population by portfolio companies is future scope (see §8).

---

## 4. The Three-Layer Architecture

The important architectural split is into three layers:

### Layer 1 — Excel Truth Layer
*What literally exists in the workbook.* (A whole host of detailed logic is required here.)

- Sheets, cells, formulas
- Styles, fills
- Locked/unlocked cells, protected sheets, hidden sheets
- Named ranges
- Validations
- Comments, text boxes, hyperlinks
- Merged cells
- Row/column metadata
- Formula dependencies

### Layer 2 — Business Interpretation Layer
*What the workbook is trying to achieve.* The LLM interprets sponsor intent.

- Sheet roles
- Section purposes
- Metric definitions
- Reporting logic, covenant logic, valuation logic
- M&A adjustment logic
- KPI logic, variance logic
- Which sheets matter, what each section collects
- Which cells are inputs vs. formulas/checks
- What periods are relevant
- What author-written rules apply

### Layer 3 — Execution Contract Layer
*What can be reused later.*

- Target fields, cells, periods
- Units, sign conventions
- Required fields
- Author rules, interpreted policies
- Validations
- Formula impact chains
- Approved field definitions

---

## 5. Why the LLM Is Critical

Sponsor templates contain nuanced PE reporting logic. The system needs to understand whether a section relates to:

**Metrics / concepts:**
- Reported EBITDA, Adjusted EBITDA, Covenant EBITDA, Valuation EBITDA
- ARR
- Pro forma M&A adjustments
- Net debt, leverage
- Liquidity, cash
- Working capital
- Forecast assumptions
- Variance bridges
- KPI movement

**Section intent / purpose:**
- Actuals
- Forecasts
- Covenant testing
- Valuation support
- Sponsor review
- Lender reporting
- M&A normalisation
- Internal consistency checks

---

## 6. MVP Output — the Reviewable Template Contract

The user sees a **grid of detected fields and sections**. Each row/field includes:

| Attribute | Description |
|---|---|
| Sheet | Which sheet the field lives on |
| Section | The section it belongs to |
| Target cell | Where the data point should go |
| Metric label | The label as written in the template |
| Canonical metric | The standardised/normalised metric |
| Period | Relevant reporting period |
| Unit | Unit of measure |
| Sign convention | Expected sign (e.g. costs negative) |
| Required / optional | Whether the field must be filled |
| Writable / input status | Whether the cell is an input |
| Business purpose | What the field/section is for |
| Author-rule evidence | Evidence of author-written rules |
| Validation evidence | Evidence from Excel validations |
| Formula dependents | Cells that depend on this field |
| Confidence | System confidence in the interpretation |
| Human review required | Whether a human must review |

**User actions on the grid:** approve · correct · reject · manually label.

---

## 7. Core Backend Objects (MVP)

- `templates`
- `template_versions`
- `template_sheets`
- `template_sections`
- `template_metric_rows`
- `template_fields`
- `template_author_rules`
- `template_policies`
- `template_validations`
- `template_contracts`
- `audit_events`

---

## 8. Future Product Flow (Out of MVP Scope)

Once a PE firm has uploaded and approved its templates, portfolio company finance teams will be able to:

1. Select the relevant sponsor template and populate it easily.
2. Upload or scan their **source workbook**.
3. Extract **source facts**.
4. **Map** those facts to approved template fields.
5. Generate **write proposals**.
6. Apply **transformations and validations**.
7. Get **user approval**.
8. **Write values** into a copy of the target workbook.
9. Generate a **full audit trail**.

---

## 9. Engineering Goal for the MVP

Prove that the system can take a **complex PE sponsor Excel template** and turn it into a **structured, reviewable, reusable contract** that explains *what the template requires and why*.

The product should make sponsor reporting templates easier for portfolio company finance teams to complete, while giving the PE firm control, consistency, and confidence in how those templates are interpreted.
