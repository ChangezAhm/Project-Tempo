"""Ingest a hand-labelled template (owner's 1/2 marks) into eval ground truth.

Owner's rule (2026-07-15):
  - `1` (typed, non-formula) = a VALUE input  -> the system should classify it data/sourced.
  - `2` (typed, non-formula) = a new FIELD    -> the system should detect an extensible
                                                  field/slot (or config), not a plain input.
  - ANY formula cell         = DISREGARD (the template computes it) — this also auto-corrects
                               the owner's slip of typing `2` onto calculated lines, because
                               those cells are formulas in the unmarked original.

Formula detection uses the NORMAL (unmarked) pair; the CLASSIFIED copy carries the marks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aspose.cells import Workbook

from app.datamodel.derive import _CONNECTOR
from app.datamodel.topology import is_multi_input


@dataclass
class LabelSet:
    inputs: set = field(default_factory=set)      # (sheet, A1) — value inputs (mark 1)
    fields: set = field(default_factory=set)      # (sheet, A1) — new-field labels (mark 2)
    disregarded_formula: int = 0                  # marks that landed on a formula → dropped
    other: int = 0                                # marks on a non-formula value that weren't 1/2


def ingest_pair(normal_path: str, classified_path: str) -> LabelSet:
    wn = Workbook(normal_path)
    wc = Workbook(classified_path)
    norm = {s.name: s for s in wn.worksheets}

    out = LabelSet()
    for ws in wc.worksheets:
        wsn = norm.get(ws.name)
        if wsn is None:
            continue
        maxr, maxc = ws.cells.max_data_row, ws.cells.max_data_column
        for r in range(maxr + 1):
            for c in range(maxc + 1):
                v = ws.cells.get(r, c).value
                if v not in (1, 1.0, 2, 2.0):
                    continue
                n = wsn.cells.get(r, c)
                if n.formula:
                    # A CONNECTOR formula (CX_GET …) FETCHES an external value, and a
                    # single-input display/transform wrapper is a TYPE-OVER default —
                    # both are inputs a fill replaces, so keep the mark. A MULTI-INPUT
                    # formula (range, ≥2 cells, aggregation) is the template's own
                    # computation → disregard (the shared write-semantics constraint).
                    keep = _CONNECTOR.search(n.formula) or not is_multi_input(n.formula)
                    if not keep:
                        out.disregarded_formula += 1
                        continue
                elif n.value == v:                       # unchanged literal — a legit original 1/2, not a mark
                    continue
                addr = ws.cells.get(r, c).name
                (out.inputs if int(v) == 1 else out.fields).add((ws.name, addr))
    return out
