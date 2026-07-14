"""Input-detection evaluation harness for Project Tempo.

The reliability audit (docs/RELIABILITY_AUDIT.md, §7) found the system had no way
to know whether input detection / cell classification improved or regressed on
real templates. This module closes that gap. It runs the DETERMINISTIC L4
derivation (derive_data_model — no LLM, free, reproducible) over golden cases and
scores it two ways:

  - INVARIANTS: specific truths verified by hand (config cells excluded, connector
    cells sourced-with-a-period, no positional 'row N' labels, totals never
    fillable). A failing invariant is a regression, full stop.
  - PRECISION / RECALL: computed only where a VERIFIED expected-input spec exists
    (constructed cases now; real templates once labelled). Honest about coverage.

Plus a per-template category-distribution BASELINE so a silent drift (inputs lost,
pollution returned) is caught even before anyone hand-labels a template.
"""
