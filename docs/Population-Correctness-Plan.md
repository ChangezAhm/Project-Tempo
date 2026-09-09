# Population Correctness — Comprehensive Plan

Status date: 2026-09-09. Owner: (founding eng + Changez). Branch: `defaulted-input-detection`.

This plan resolves the five failure classes found on the Template-1 real-data run
(Meridian mock source → Harbourline flash template), traces each to code, and
sequences fixes so the *class* of error dies, not the instance. Every fix is
validated against the marked ground truth in `Templates for testing/`.

## The unifying diagnosis

Every failure is the **same architectural inversion**: deterministic *judgment*
code (lexicons, precedence heuristics, one-winner binding) is deciding what cells
*mean*, and overriding or pre-empting the LLM's understanding — with nothing that
verifies the deterministic judgment against hard facts. This violates the project's
own doctrine ("LLM owns meaning, deterministic owns facts; rules doing the LLM's
judgment job = the failure"). `DERIVATION_VERSION` reaching 19 is the smoking gun:
the grain rule has been re-patched ~19 times, each version fixing one template and
breaking another.

**Two kinds of deterministic code, and they are opposites:**
- Deterministic **judgment** (deciding meaning) — the disease. Remove it / stop it
  overriding the LLM.
- Deterministic **verification** (checking meaning against facts, refusing
  contradictions) — the cure. The scale guard is this; extend the pattern.

The fix is to redraw the boundary back to the doctrine: judgment → LLM grounded on
facts; facts + contradiction-guards → deterministic.

## The five issues → root cause → fix

| # | Symptom | Root cause (file:line) | Fix phase |
|---|---|---|---|
| 1 | Jan of each year = full-year total (Rev 29,132 vs 2,063) | `derive.py:789` precedence: a year-grouping band over the first month tags the column `year`; executor sums 12 months (`periods.py:206`). Confirmed at runtime: Q10/E10/AC10 = `year`. | **P1 (DONE)** |
| 5 | Budget FY26 empty | NOT period recognition (AO10 *is* `year`+`budget`). One-winner binding picked a non-budget/non-annual series; annual budget source never mapped. | P3 |
| 3 | Pre-2024 history empty | One series wins per metric (`mapping.py:44`); execute confined to that series' own sheet (`execute.py:307-313`); Outlook history never consulted; blank cells emitted with `no_column_in_bucket`. | P3 |
| 2 | EBITDA adjustment lines unfilled | `priors.py:36` regex flags "Adjustment N" → placeholder → `config` → dropped (`demand.py:72`); LLM input-claim can't rescue a blank cell (`derive.py` type-over only rescues formulas). | P2 |
| 4 | Company text fields unfilled | `input_detector.py:116` fires only for NUMBER/DATE; blind to text inputs. | P2 |

## Phase 1 — Grain band guard (DONE, tested)

**Change:** `derive.py` — a `year` label on a column that sits inside a run of ≥3
consecutive monthly columns is a grouping band, not the column's grain; demote it
to the sheet's monthly grain. Standalone FY columns are never in a run, so they
keep `year`. New helper `_monthly_run_cols`. `DERIVATION_VERSION` → 20.

**Why this is not pendulum rule #20:** it does not re-order the LLM-vs-date
precedence (which is what kept breaking). It adds one *fact* — "is this column part
of a consecutive monthly sequence?" — that distinguishes a band from a real annual
column, the exact ambiguity every prior version guessed at.

**Validation:**
- Offline re-derive of the real template: E/Q/AC (Jan 2023/24/25) `year → monthly`;
  AO/AP (Budget FY26, FY25 Actual) stay `year`. ✓
- 5 unit tests (`tests/test_grain_band.py`): band-in-run, standalone-FY,
  adjacent-FY-with-gap, two-month stub, quarterly dates. ✓
- Full suite 429 passed. ✓

**Residual (tracked):** a genuine FY-total column whose header is a bare year-end
*date* AND which is immediately adjacent to a monthly block with no gap could still
be ambiguous. Handled by P4's contradiction guard, not more precedence rules.

## Phase 2 — Stop deterministic priors overriding LLM input judgment

Two targeted inversions so the LLM's meaning wins over a lexicon/numeric prior:

**2a. Placeholder lexicon must not veto an LLM input-claim on a blank cell.**
Today `_classify_category` stamps "Adjustment 1/2/3" `config/placeholder` and the
LLM calling them `input_fields` cannot rescue a *blank* cell (the type-over
inversion at `derive.py` requires `category == "computed"`). Change: when the LLM's
understanding explicitly lists a blank cell as an `input_field` in a data section,
that claim beats the placeholder prior → `data`. Keep the prior only where the LLM
is silent. (The lexicon stays as a gap-filler, never an override.)

**2b. Text input fields.** `input_detector.py` signal-5 recognises inputs only for
NUMBER/DATE cells. Add: a blank cell the LLM names as a text `input_field` (or an
unlocked, dependent-free cell in an input section) becomes a text `data` fact.
`apply.py` already writes non-numeric values through unchanged, so the write path
needs no change.

**Validation:** re-derive the marked Template-1; assert the Adjustment 1/2/3 rows
and the company text field move from `config`/absent into `fillable`, scored against
the marked (1/2) ground truth via `score_detection.py`. Target: P&L input recall
100% (was 70.1% — the entire gap is the 111 adjustment cells).

**Risk:** loosening the placeholder veto could re-admit genuine scaffolding rows.
Mitigate by requiring an explicit LLM `input_field` claim (not mere label
absence), and re-run detection precision across all three marked templates to
confirm false-inputs don't rise.

## Phase 3 — Coverage: one metric may need more than one source sheet

**3a. Loud coverage accounting (cheap, do first).** Today unused sheets/series are a
priority-7 "is that expected?" line with no hint they held missing periods. Change
`report.py` to emit a **blocking-visible** coverage statement whenever a *catalogued*
source series covers periods the template demanded but the winning series lacked —
e.g. "Revenue filled from T1 Output (2024-01..2026-12); Outlook holds 2019..2023 for
the same metric — 12 template months left blank. Use it?" This converts silent gaps
(#3, #5) into a decision.

**3b. Cross-sheet history stitch (bigger).** When the winning series misses a
demanded period that another catalogued series of the *same metric* covers, allow
execute to source that period from the second series — under the same grain/scale/
scenario guards, with provenance recorded per cell. This is the real fix for #3 and
#5. Design: extend the mapper contract to allow a `history_series_id` (or let
execute fall back across same-metric series ranked by coverage), gated so a fallout
across sheets still passes reconciliation.

**Validation:** re-run Template-1 fill; assert 2023 columns populate from Outlook
and Budget FY26 populates from the annual budget series; zero silent blanks where
data exists; every cross-sheet fill carries provenance.

## Phase 4 — Grain/coverage contradiction guards (the safety net)

Deterministic *verification* (the good kind of deterministic code), added at the
verify stage so LLM/derivation judgment is caught when wrong:
- **Grain contradiction:** a slot tagged annual that sits inside a monthly run, or a
  value whose source period ≠ the slot's period, is refused/flagged (backstops P1's
  residual without a new precedence rule).
- **Coverage contradiction:** a demanded period with data available in *some*
  catalogued series but left blank is a flagged gap, never silent (backstops P3).

These make the LLM-judgment phases safe: judgment can err, but errors surface as
loud refusals, not shipped numbers — the scale-guard pattern generalised.

## Verification protocol (every phase)

1. Unit tests for the new logic.
2. Offline `derive_data_model` re-derive of the real template; assert the specific
   cells changed as intended and nothing else regressed.
3. `score_detection.py` against the marked (1/2) ground truth — recall & precision
   per template; must not drop on the two templates a phase doesn't target.
4. Full `pytest` suite green.
5. (P3+) a live fill of Template-1, cell-scored against the marked file.

## Sequencing & rationale

P1 first — highest damage (silent wrong numbers), already done. P2 next — pure
derivation change, testable offline against the marked file, no fill needed. P3
after — needs the coverage accounting (3a) before the stitch (3b). P4 last — it
backstops the judgment phases and is cheapest to add once their shapes are known.
Each phase commits independently with its tests.

## Doctrine checkpoint

After each phase, confirm the change *removed* deterministic judgment or *added*
deterministic verification — never added deterministic judgment. If a fix needs a
new precedence rule to decide meaning, it is wrong; route that decision to the LLM
and add a contradiction guard instead.
