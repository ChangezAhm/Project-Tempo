"""Score the system's classification against a hand-labelled template.

Given the owner's ground truth (from label_ingest.ingest_pair) and the system's
derived facts for the SAME template, compute precision/recall for:
  - VALUE-INPUT detection: did the system classify the '1' cells as fillable
    (data/sourced)?  Recall = it found them; Precision = it didn't over-call.
  - NEW-FIELD detection: did the system flag the '2' cells as an extensible
    region slot or config (not a plain input, not a computed cell)?

Matching is by (sheet, cell), so the system must be onboarded from the SAME
(NORMAL) file the marks were made on.
"""

from __future__ import annotations

from dataclasses import dataclass

_FILLABLE = ("data", "sourced")
_FIELD_OK = ("config",)          # + region membership, passed in separately


@dataclass
class Score:
    label: str
    n_marked_inputs: int
    n_marked_fields: int
    # value inputs
    input_tp: int
    input_fp: int
    input_fn: int
    input_precision: float
    input_recall: float
    input_f1: float
    # new fields
    field_found: int
    field_recall: float

    def as_lines(self) -> list[str]:
        return [
            f"{self.label}",
            f"  VALUE INPUTS  (you marked {self.n_marked_inputs}): "
            f"precision={self.input_precision:.0%}  recall={self.input_recall:.0%}  F1={self.input_f1:.0%}"
            f"   [found {self.input_tp}, missed {self.input_fn}, over-called {self.input_fp}]",
            f"  NEW FIELDS    (you marked {self.n_marked_fields}): "
            f"recall={self.field_recall:.0%}   [found {self.field_found}]",
        ]


def _key(sheet, cell):
    return (sheet, str(cell).upper())


def score(label: str, gt_inputs: set, gt_fields: set, facts: list[dict],
          region_cells: set | None = None) -> Score:
    """gt_inputs / gt_fields: {(sheet, A1)} from ingest_pair.
    facts: the system's DataPoints (dicts w/ sheet_name, cell, category).
    region_cells: {(sheet, A1)} the system flagged as extensible-region slots."""
    region_cells = region_cells or set()
    sys_cat = {_key(f["sheet_name"], f.get("cell")): f.get("category") for f in facts}
    sys_fillable = {k for k, c in sys_cat.items() if c in _FILLABLE}

    gi = {_key(s, c) for s, c in gt_inputs}
    gf = {_key(s, c) for s, c in gt_fields}

    tp = len(sys_fillable & gi)
    fp = len(sys_fillable - gi - gf)          # system called it an input, you marked nothing
    fn = len(gi - sys_fillable)               # you marked an input, system didn't make it fillable
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0

    # a marked field is "found" if the system flagged it config OR put it in a region slot
    field_found = sum(1 for k in gf
                      if sys_cat.get(k) in _FIELD_OK or k in {_key(s, c) for s, c in region_cells})
    frec = field_found / len(gf) if gf else 1.0

    return Score(label, len(gi), len(gf), tp, fp, fn, prec, rec, f1, field_found, frec)
