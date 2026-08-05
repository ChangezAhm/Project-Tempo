"""The ONLY LLM step in population: text-only meaning matching.

Given the template's distinct metrics and the deterministic source catalogue
(labels only — no values, no images), ask the model which source series *means*
which template metric, and whether the sign convention differs. That's it. No
addresses, no numbers, no scale, no periods — those are all decided deterministically
in binding.py from facts Aspose already knows.

This is what makes runaway cost structurally impossible for the populate path:
one cheap (Sonnet) text call (chunked if huge), under the run's spend cap, with a
dry-run estimate available before a single token is sent.
"""

from __future__ import annotations

import json
import logging

from app.llm import MODEL_MAP, guarded_stream
from app.population.catalogue import Series
from app.population.cost import estimate_call_usd
from app.population.schema import MappingOut, MetricMap

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You map a consulting TEMPLATE's metrics to a SOURCE workbook's data series by "
    "FINANCIAL MEANING. You get the template metrics and a catalogue of source series "
    "(each a labelled row). For EACH template metric, choose one STATUS and fill the "
    "fields for it. The template and the source almost NEVER share the exact same "
    "breakdown — your job is to reconcile them intelligently, not to give up.\n"
    "\nSTATUS — pick exactly one per metric:\n"
    "- direct: a single source series means the same thing -> set series_id.\n"
    "- aggregate: the metric is the EXACT arithmetic SUM of several source series and no "
    "single source series already means it (e.g. 'Total Revenue' when the source lists "
    "only 'Revenue - NA / EMEA / APAC') -> series_id=first, also_series_ids=the rest. "
    "Exact; filled automatically.\n"
    "- reconcile: the source HAS the same economic amount but cut DIFFERENTLY than the "
    "template wants — the data EXISTS, just organised differently. Assign the best source "
    "data to THIS line (one series, or a sum via also_series_ids) and write a short "
    "`assumption`. Use reconcile when:\n"
    "   * the source COMBINES what the template SPLITS — e.g. one 'Depreciation & "
    "amortisation' line: reconcile it onto Depreciation (assumption 'source combines D&A; "
    "assigned to depreciation'); mark Amortisation unavailable (note 'included in the "
    "combined D&A on Depreciation').\n"
    "   * the source SPLITS on a DIFFERENT axis — e.g. opex by function (S&M, G&A) vs the "
    "template's by-nature Staff/Other: SUM the functional lines (also_series_ids) onto the "
    "template's RESIDUAL/other line (assumption 'source splits opex by function; summed "
    "into other opex'); mark the more specific line (Staff Costs) unavailable (note "
    "'source does not separate staff costs; included in other opex').\n"
    "   * the source gives a TOTAL where the template wants a COMPONENT — assign the total "
    "to the dominant component; mark the siblings unavailable.\n"
    "  Reconcile fills are PROVISIONAL: they are flagged and sent to the user to confirm. "
    "The value still comes from a real source series — NEVER invent numbers. NEVER map the "
    "same source amount into two template lines (no double counting — that is why the "
    "sibling line is marked unavailable).\n"
    "- needs_decision: LAST RESORT — only when the source has related data but NO "
    "component is a defensible default, so any assignment would be a coin-flip. If one "
    "component is clearly the main line (e.g. recurring revenue for a subscription "
    "business, especially when an ARR/subscription metric exists), do NOT use this — "
    "prefer reconcile: assign the total to that dominant component and mark the siblings "
    "unavailable (fill provisionally, flagged). Use needs_decision only for a genuine "
    "coin-flip: series_id=null, put the QUESTION + options in `assumption`; we ASK, we do "
    "NOT fill.\n"
    "- unavailable: the source has NO data for this line, not even at a different cut "
    "(e.g. a cash-flow line when the source has no cash-flow statement) -> series_id=null "
    "and put a SPECIFIC reason in `note`.\n"
    "\nRULES:\n"
    "- NO DOUBLE COUNTING (critical): each source series may be used by AT MOST ONE "
    "template metric. Never put the same source amount into two template lines, and never "
    "use a source series both on its own AND inside another line's aggregate. When one "
    "source line's amount belongs to a template line that AGGREGATES others (the "
    "residual/total, e.g. Other Opex = S&M + G&A), THAT line owns those source series and "
    "the more specific template lines (e.g. Staff Costs) are unavailable.\n"
    "- Match meaning, not wording: 'Total revenue'=='Net sales'; 'COGS'=='Cost of sales'.\n"
    "- A metric may carry `def:`/`qualifies:` from the template. For a DIRECT match they "
    "are binding: if a source series fails the qualification, do not map it directly. You "
    "MAY still reconcile with an explicit assumption.\n"
    "- A metric may carry `source:` — the provenance the template EXPECTS (e.g. "
    "'management accounts', 'deal model', 'Flash Report'). Prefer source series consistent "
    "with it; when the uploaded workbook is clearly the WRONG kind for a metric, mark that "
    "metric unavailable and say so in `note` rather than forcing a match.\n"
    "- A TEMPLATE CONTEXT block, when present, carries sponsor-confirmed rules and answered "
    "review questions — it is AUTHORITATIVE. If it states how to reconcile a line, follow "
    "it exactly (that is a human decision that must win over your own).\n"
    "\nYOU DECIDE THE COMPLETE FILL SEMANTICS per metric — deterministic code executes "
    "your plan and verifies it against the workbook facts; it does not re-decide it:\n"
    "- rollup: how the metric aggregates from finer to coarser periods (months -> "
    "quarter/year) when grains differ: 'sum' for period flows (revenue, costs, cash "
    "flow), 'end' (period-end value) for point-in-time stocks (balance-sheet items, "
    "headcount/FTE, customer counts, ARR/MRR and other run-rates), 'avg' for "
    "rates/ratios/percentages (churn %, margins, conversion). Set it for every mapped "
    "metric.\n"
    "- source_unit / target_unit: the units AS YOU READ THEM from the source series "
    "(its label, unit tag, or the sheet's banner — e.g. \"USD'000\") and from the "
    "template metric (its unit/format — e.g. 'EUR m', '%', 'FTE'). Echo the unit "
    "STRINGS — the executor computes the scale factor and cross-checks it against the "
    "template's real magnitudes; never compute or state a scale yourself. A sheet "
    "banner like \"all figures USD'000\" applies to money rows, NOT to counts, "
    "percentages or ratios on the same sheet.\n"
    "- sign_flip + sign_basis: set sign_flip=true only when conventions differ (source "
    "costs +ve, template enters costs -ve); sign_basis = one line saying what told you "
    "(the template's sign convention note, the sign of existing values, the label).\n"
    "- scenario: when the template demands budget/forecast slots for a metric, emit a "
    "separate mapping per demanded scenario (same metric key) naming which scenario it "
    "serves; omit for plain actuals.\n"
    "- period_map: 'calendar' (default — align by real dates) or 'positional' ONLY "
    "when the facts show one side has no readable dates.\n"
    "\nTYPICAL TREATMENTS (suggestions from experience — NOT rules; when the facts "
    "contradict one, trust the facts and say so in note): balance-sheet sections are "
    "stocks ('end'); P&L/cash-flow sections are flows ('sum'); percentages/ratios "
    "average; headcount/FTE/customer counts are stocks and are dimensionless (unit "
    "'count' — never a money scale).\n"
    "\n- confidence in [0,1]. For direct/aggregate, <0.6 becomes a user question (with "
    "your mapping as the suggestion) instead of a fill. reconcile is kept "
    "(provisional) but still score it honestly.\n"
    "- NEVER output numeric cell values, cell addresses, computed scale factors, or "
    "currency conversions — only the semantic plan.\n"
    'Return ONLY JSON: {"mappings":[{"metric":"...","status":"direct|aggregate|reconcile|'
    'needs_decision|unavailable","series_id":"...|null","also_series_ids":[],'
    '"assumption":"...|null","rollup":"sum|end|avg","scenario":"actual|budget|forecast|null",'
    '"source_unit":"...|null","target_unit":"...|null","sign_flip":false,'
    '"sign_basis":"...|null","period_map":"calendar|positional","confidence":0.0,'
    '"note":"..."}]}'
)

_BATCH = 12  # template metrics per call; the full series catalogue + slot facts ride
             # along each time, and each metric now gets a complete fill-semantics
             # answer — small batches keep the output JSON inside max_tokens.


def _series_periods(s: Series) -> str:
    """Compact facts about a series' timeline: grain(s), date range, scenario mix
    — the planner decides grain correspondence from these, so they must be real."""
    dated = [(d, pt) for (_c, d, pt) in s.period_cols if d is not None]
    if not dated:
        return f"{len(s.period_cols)} undated column(s)" if s.period_cols else "no period columns"
    grains = sorted({(pt or "?") for _d, pt in dated})
    lo, hi = min(d for d, _ in dated), max(d for d, _ in dated)
    scen = sorted({(s.col_scenario.get(c) or "actual") for (c, d, _pt) in s.period_cols if d is not None})
    return f"{'/'.join(grains)} {lo:%Y-%m}..{hi:%Y-%m} ({len(dated)} cols, {'/'.join(scen)})"


def _series_lines(catalogue: dict[str, Series]) -> str:
    lines = []
    for s in catalogue.values():
        sample = ", ".join(f"{v:g}" for v in s.sample) if s.sample else ""
        unit = []
        if s.unit.currency:
            unit.append(s.unit.currency)
        if s.unit.base and s.unit.base != 1.0:
            unit.append(f"x{s.unit.base:g}")
        if s.unit.kind and s.unit.kind != "unknown":
            unit.append(s.unit.kind)
        meta = f" [{'/'.join(unit)}]" if unit else ""
        lines.append(f"{s.id} | {s.sheet} | {s.label}{meta} | {_series_periods(s)}"
                     + (f" | e.g. {sample}" if sample else ""))
    return "\n".join(lines)


def _metric_lines(metrics: list[dict]) -> str:
    from app.population.units import resolve_unit
    out = []
    for m in metrics:
        label = m.get("label") or m.get("metric")
        unit = m.get("unit")
        kind = resolve_unit(unit).kind
        meta = f" | unit: {unit}" if unit else ""
        if kind != "unknown":
            meta += f" ({kind})"
        sign = m.get("sign_convention")
        if sign:   # the template's own convention — informs the sign_flip guess
            meta += f" | sign: {str(sign)[:60]}"
        # The L3 business logic — what the line MEANS and what QUALIFIES to be in
        # it — is exactly what separates 'Adjusted' from 'Reported' EBITDA. The
        # mapper was deciding on labels alone while this sat unused in the model.
        if m.get("definition"):
            meta += f" | def: {str(m['definition'])[:90]}"
        if m.get("qualification_criteria"):
            meta += f" | qualifies: {str(m['qualification_criteria'])[:110]}"
        # provenance the template expects ("management accounts", "deal model") —
        # lets the mapper prefer/refuse a source of the wrong kind.
        if m.get("expected_source"):
            meta += f" | source: {str(m['expected_source'])[:60]}"
        # the template SLOTS this metric must fill: per-sheet grain + date range +
        # section context — the facts the planner's grain/rollup call rests on.
        if m.get("slots"):
            meta += f" | slots: {str(m['slots'])[:160]}"
        if m.get("section_type"):
            meta += f" | section: {m['section_type']}"
        out.append(f"{m.get('metric')} | {label}{meta}")
    return "\n".join(out)


def _user_text(metrics: list[dict], series_block: str, context: str = "") -> str:
    ctx = f"TEMPLATE CONTEXT (authoritative — sponsor-confirmed):\n{context}\n\n" if context else ""
    return (
        f"{ctx}"
        "SOURCE SERIES (id | sheet | label [unit] | samples):\n"
        f"{series_block}\n\n"
        "TEMPLATE METRICS to map (key | label | unit | def | qualifies):\n"
        f"{_metric_lines(metrics)}\n\n"
        "Return the JSON now."
    )


def estimate_mapping_usd(metrics: list[dict], catalogue: dict[str, Series],
                         max_tokens: int = 8000, context: str = "") -> float:
    """Dry-run cost: what the whole mapping step would cost before sending anything."""
    series_block = _series_lines(catalogue)
    total = 0.0
    for i in range(0, len(metrics), _BATCH):
        chunk = metrics[i:i + _BATCH]
        chars = len(_SYSTEM) + len(_user_text(chunk, series_block, context))
        total += estimate_call_usd(MODEL_MAP, chars, max_tokens)
    return round(total, 4)


def _parse(text: str) -> list[MetricMap]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return []
    return MappingOut(**json.loads(text[start:end + 1])).mappings


def _map_chunk(chunk: list[dict], series_block: str, max_tokens: int,
               context: str = "") -> list[MetricMap]:
    """One mapping batch, with ONE corrective retry when the reply doesn't parse
    (or parses to nothing for a non-empty chunk). Raises after the retry fails —
    the caller decides whether that kills the run."""
    user = _user_text(chunk, series_block, context)
    _, text = guarded_stream(model=MODEL_MAP, system=_SYSTEM, content=user,
                             max_tokens=max_tokens, est_input_chars=len(_SYSTEM) + len(user),
                             temperature=0, site="metric_planner")
    try:
        maps = _parse(text)
        if maps:
            return maps
        err = "reply contained no mappings"
    except Exception as e:  # noqa: BLE001 — malformed JSON from the model
        err = str(e)
    logger.warning("mapping batch didn't parse (%s) — one corrective retry", err)
    messages = [
        {"role": "user", "content": user},
        {"role": "assistant", "content": text[:4000]},
        {"role": "user", "content": (
            f"That reply was not usable ({err}). Return ONLY the JSON object "
            '{"mappings":[...]} for the TEMPLATE METRICS above — no prose, no fences.'
        )},
    ]
    _, text = guarded_stream(model=MODEL_MAP, system=_SYSTEM, messages=messages,
                             max_tokens=max_tokens, temperature=0, site="metric_planner_retry")
    maps = _parse(text)
    if not maps:
        raise RuntimeError("mapping batch unusable after corrective retry")
    return maps


def repair_plan(failing: list[tuple[dict, "MetricMap", list]], catalogue: dict[str, Series],
                max_tokens: int = 8000, context: str = "") -> list[MetricMap]:
    """ONE self-repair round (Fill-Plan §2.4 tier 1): the planner sees its own
    previous entries plus the verifier's typed findings for ONLY the failing
    metrics, and returns revised entries. Best-effort — a failed repair leaves
    the original entries (and their issues) standing for the question tier.

    failing: [(metric_dict, previous_fill, [PlanIssue-like dicts/objects])]."""
    from app.population.cost import SpendCapExceeded

    if not failing or not catalogue:
        return []
    blocks = []
    for metric, prev, issues in failing:
        prob = "; ".join(f"{i.code}: {i.detail}" for i in issues)
        blocks.append(f"METRIC:\n{_metric_lines([metric])}\n"
                      f"YOUR PREVIOUS PLAN: {json.dumps(prev.model_dump(exclude_none=True))}\n"
                      f"VERIFIER FINDINGS: {prob}")
    user = (
        (f"TEMPLATE CONTEXT (authoritative — sponsor-confirmed):\n{context}\n\n" if context else "")
        + "SOURCE SERIES (id | sheet | label [unit] | periods | samples):\n"
        + _series_lines(catalogue)
        + "\n\nThe deterministic verifier could not execute these plan entries. "
          "Revise EACH one to something executable against the facts above (or mark it "
          "needs_decision/unavailable with a clear reason — never force a bad fill):\n\n"
        + "\n\n".join(blocks)
        + "\n\nReturn the corrected JSON now (mappings for ONLY these metrics)."
    )
    try:
        _, text = guarded_stream(model=MODEL_MAP, system=_SYSTEM, content=user,
                                 max_tokens=max_tokens, temperature=0, site="plan_repair")
        return _parse(text)
    except SpendCapExceeded:
        raise
    except Exception as e:  # noqa: BLE001 — repair is best-effort by design
        logger.warning("plan repair round failed (%s) — issues fall through to questions", e)
        return []


def map_metrics(metrics: list[dict], catalogue: dict[str, Series],
                max_tokens: int = 8000, context: str = "") -> tuple[list[MetricMap], list[str]]:
    """Run the mapping (chunked). ``context`` is the template's business-context
    block (sponsor notes, answered review questions, strict rules) — authoritative
    knowledge the mapper must honor. Returns (mappings, failed_metric_keys). A
    batch whose retry also fails is dropped LOUDLY — the affected METRIC KEYS are
    returned so the run report can name exactly what went unmapped and why —
    instead of either silently vanishing or killing a paid run. Guarded by the
    run's spend cap inside guarded_stream; a SpendCapExceeded still aborts
    everything."""
    from app.population.cost import SpendCapExceeded

    if not metrics or not catalogue:
        return [], []
    series_block = _series_lines(catalogue)
    out: list[MetricMap] = []
    failed_metrics: list[str] = []
    for i in range(0, len(metrics), _BATCH):
        chunk = metrics[i:i + _BATCH]
        try:
            out.extend(_map_chunk(chunk, series_block, max_tokens, context))
        except SpendCapExceeded:
            raise
        except Exception:  # noqa: BLE001
            failed_metrics.extend(str(m.get("metric")) for m in chunk)
            logger.exception("mapping batch %d-%d failed after retry — %d metrics unmapped",
                             i, i + len(chunk), len(chunk))
    return out, failed_metrics
