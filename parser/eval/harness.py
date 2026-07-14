"""Core harness: run derive over a case, score invariants + precision/recall."""

from __future__ import annotations

import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from app.datamodel import derive as _derive

_FILLABLE = ("data", "sourced")
_ROWN = re.compile(r"^row \d+$")


# --------------------------------------------------------------------------- #
# Running derivation (real template or a constructed in-memory case)
# --------------------------------------------------------------------------- #
def facts_for_template(template_id: str) -> list[dict]:
    """Run the real deterministic derivation over a stored template (uses the
    persisted understanding/structure/snapshot — NO LLM call)."""
    res = _derive.derive_data_model(template_id)
    return [f.model_dump() if hasattr(f, "model_dump") else f for f in res.facts]


@contextmanager
def _patched(understanding: dict, structure: dict, snapshot: dict):
    """Feed derive_data_model constructed inputs instead of the DB/storage."""
    g_und = _derive.get_understanding
    g_str = _derive.get_structure
    g_snap = _derive._load_snapshot
    _derive.get_understanding = lambda tid: understanding
    _derive.get_structure = lambda tid: structure
    _derive._load_snapshot = lambda vid, tid: snapshot
    try:
        yield
    finally:
        _derive.get_understanding = g_und
        _derive.get_structure = g_str
        _derive._load_snapshot = g_snap


def facts_for_inline(understanding: dict, structure: dict, snapshot: dict) -> list[dict]:
    """Run the real derivation over a CONSTRUCTED case (fast, no DB)."""
    with _patched(understanding, structure, snapshot):
        res = _derive.derive_data_model("inline")
    return [f.model_dump() if hasattr(f, "model_dump") else f for f in res.facts]


# --------------------------------------------------------------------------- #
# Case + scoring
# --------------------------------------------------------------------------- #
Invariant = Callable[[list[dict]], "InvariantResult"]


@dataclass
class InvariantResult:
    name: str
    ok: bool
    detail: str = ""
    advisory: bool = False   # a faithfulness/quality signal, not a hard regression gate


@dataclass
class Case:
    name: str
    # exactly one source:
    template_id: str | None = None
    inline: tuple[dict, dict, dict] | None = None      # (understanding, structure, snapshot)
    invariants: list[Invariant] = field(default_factory=list)
    # verified ground truth for precision/recall: {(sheet, cell): expected_category}
    expected: dict[tuple[str, str], str] | None = None
    description: str = ""


@dataclass
class CaseResult:
    name: str
    ran: bool
    error: str | None
    n_facts: int
    categories: dict[str, int]
    invariants: list[InvariantResult]
    precision: float | None
    recall: float | None
    f1: float | None
    category_accuracy: float | None

    @property
    def passed(self) -> bool:
        """Hard pass: derived cleanly and every non-advisory invariant holds."""
        return (self.ran and self.error is None
                and all(i.ok for i in self.invariants if not i.advisory))


def _cell_key(f: dict) -> tuple[str, str]:
    return (f.get("sheet_name"), (f.get("cell") or "").upper())


def score_precision_recall(facts: list[dict], expected: dict[tuple[str, str], str]):
    """Treat 'is this cell a fillable input?' as the positive class.
    Returns (precision, recall, f1, category_accuracy)."""
    exp = {(s, c.upper()): cat for (s, c), cat in expected.items()}
    exp_inputs = {k for k, cat in exp.items() if cat in _FILLABLE}
    got_cat = {_cell_key(f): f.get("category") for f in facts}
    got_inputs = {k for k, cat in got_cat.items() if cat in _FILLABLE}

    tp = len(got_inputs & exp_inputs)
    precision = tp / len(got_inputs) if got_inputs else (1.0 if not exp_inputs else 0.0)
    recall = tp / len(exp_inputs) if exp_inputs else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    # category accuracy over every labelled cell that derive emitted a fact for
    judged = [(k, cat) for k, cat in exp.items() if k in got_cat]
    cat_acc = (sum(1 for k, cat in judged if got_cat[k] == cat) / len(judged)) if judged else None
    return precision, recall, f1, cat_acc


def run_case(case: Case) -> CaseResult:
    try:
        if case.template_id:
            facts = facts_for_template(case.template_id)
        elif case.inline:
            facts = facts_for_inline(*case.inline)
        else:
            raise ValueError("case has neither template_id nor inline")
    except Exception as e:  # noqa: BLE001 — a case that can't derive is a reportable failure
        return CaseResult(case.name, ran=False, error=str(e)[:200], n_facts=0,
                          categories={}, invariants=[], precision=None, recall=None,
                          f1=None, category_accuracy=None)

    cats = dict(Counter(f.get("category") for f in facts))
    inv = [fn(facts) for fn in case.invariants]
    p = r = f1 = ca = None
    if case.expected:
        p, r, f1, ca = score_precision_recall(facts, case.expected)
    return CaseResult(case.name, ran=True, error=None, n_facts=len(facts),
                      categories=cats, invariants=inv, precision=p, recall=r,
                      f1=f1, category_accuracy=ca)
