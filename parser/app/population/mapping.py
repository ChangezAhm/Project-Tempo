"""The mapping brain of population: the model PERCEIVES, code VERIFIES.

Three variants live here, selected by the pipeline (TEMPO_MAPPER + gates):
- ONE-PASS (default, ≤80 metrics): `understand_and_map` — one strong-model call
  that reads BOTH workbooks as address-tagged grids (+ one layout image per
  sheet) and returns source-structure claims AND metric mappings together.
- BATCHED GRID: `map_metrics` with grids — the same full-workbook context,
  chunked with a cached stable prefix, auto-tiered to Sonnet above
  TEMPO_OPUS_METRIC_LIMIT metrics.
- DIGEST (legacy fallback, TEMPO_MAPPER=digest): text-only label matching
  against the catalogue, no grids/images.

Whatever variant runs, the OUTPUT contract is identical — MetricMap entries
naming catalogue series ids (or label-cell refs resolved by
`translate_sources`) — and every number still comes from the snapshot via
verify/execute/apply: the model never emits a value, address arithmetic, scale
or period alignment. Bounded revision calls (`revise_plan` outcome loop,
`revise_for_checks` tie-out loop, `repair_plan`) reuse the same contract.
All calls run under the run's spend cap, with a dry-run estimate available
before a single token is sent.
"""

from __future__ import annotations

import json
import os
import re
import logging

from app.llm import MODEL_MAP, MODEL_SMART, guarded_stream
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
    "  ANCHOR RULE (hard): a statement's top-line block (revenue, total assets, total "
    "costs…) must NEVER end up entirely unavailable while the source carries the "
    "corresponding economic amount in ANY cut (a single total, a different split like "
    "geography) — an empty anchor poisons every subtotal below it. Reconstruct it and "
    "use status=RECONCILE (not aggregate): placing a different cut onto a component "
    "line is a reconciliation, and a reconcile FILLS at any honestly-scored confidence "
    "(flagged for the user) — an aggregate below 0.6 is held back, which for an anchor "
    "means an empty statement. In general: whenever your best anchor mapping scores "
    "below 0.6, express it as reconcile with the assumption stated. Also never spend a "
    "source series on a DERIVED metric (growth %, ratios) while the raw-value line that "
    "series directly represents goes unfilled — raw values first.\n"
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
    "- coverage_series_ids: when the template demands periods the primary series_id does "
    "NOT cover — earlier YEARS of history, or a budget/forecast year on another sheet — "
    "list EVERY OTHER source series that represents this SAME metric, even when its label "
    "differs (e.g. 'Total revenue (allocated)' on a history sheet vs the template's "
    "'Revenue'). Execute fills only the missing periods from these, under the same guards. "
    "This is NOT also_series_ids: also_series_ids SUMS components into a total on the same "
    "sheet; coverage_series_ids are alternative sources of the SAME line for DIFFERENT "
    "periods/scenarios. Leave [] when the primary series already covers every demanded period.\n"
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
    '"coverage_series_ids":[],'
    '"assumption":"...|null","rollup":"sum|end|avg","scenario":"actual|budget|forecast|null",'
    '"source_unit":"...|null","target_unit":"...|null","sign_flip":false,'
    '"sign_basis":"...|null","period_map":"calendar|positional","confidence":0.0,'
    '"note":"..."}]}'
)

_BATCH = 12  # template metrics per call; the full series catalogue + slot facts ride
             # along each time, and each metric now gets a complete fill-semantics
             # answer — small batches keep the output JSON inside max_tokens.

# GRID MODE (the Tracelight-parity inversion): the mapper SEES both workbooks —
# full address-tagged grids — and maps in ONE strong-model call. The output
# contract is IDENTICAL (MetricMap entries keyed by catalogue series ids), so
# verify/execute/apply and every guard downstream are unchanged: the model
# gained eyes, not hands. Rationale: every mapping-quality failure traced to
# the model being shown a starved digest while deterministic code did the
# 'seeing' badly; values/magnitudes/layout are the strongest mapping evidence
# that exists and they were being withheld.
_GRID_PREAMBLE = (
    "You can SEE both workbooks below as full grids (address = value, exactly as the "
    "sheets display; values are authoritative). Map like an analyst with both files "
    "open:\n"
    "- READ the actual numbers: magnitudes reveal scale and units, signs reveal "
    "conventions, and a candidate mapping should be sanity-checked by comparing a few "
    "real values between the sheets.\n"
    "- READ the layout: period axes (columns OR rows), scenario blocks, fiscal labels, "
    "section structure. Trust what the grid shows over any summary.\n"
    "- The SOURCE SERIES catalogue lists the ids you must use in series_id / "
    "also_series_ids — pick ids whose grid rows/columns you have verified by looking. "
    "If data you can see in the grid has NO catalogue id, do not invent one: say so in "
    "the affected metric's note.\n"
    "- Every id and semantic you output is verified and executed deterministically; "
    "never output cell values or computed scales.\n\n"
)
_GRID_MAX_TOKENS = 24000
_GRID_HALF = 40   # metrics per call in grid mode (halved only for huge templates)

# ONE-PASS mode: the same call that maps ALSO reports the source's structure
# (periods + series per sheet, the existing claim schema) — the model reads the
# source once, with the demand in hand, instead of a separate digest-fed
# understanding pass. Mappings reference series by their LABEL CELL (visible in
# the grid); the catalogue's own orientation logic turns cells into ids after
# the fact, so the model never guesses id formats.
_ONEPASS_CONTRACT = (
    "FIRST report each SOURCE sheet's structure, THEN the mappings. Return ONLY JSON:\n"
    '{"sheets":[{"sheet":"...","periods":[{"header_cell":"B5","date":"YYYY-MM-DD or null",'
    '"grain":"month|quarter|year|ltm|ytd","kind":"actual|budget|forecast"}],'
    '"series":[{"label_cell":"B10","label":"...","unit":"...|null","currency":"...|null",'
    '"scenario":"actual|budget|forecast|null","variant_of":"...|null"}]}],'
    '"mappings":[{"metric":"...","status":"direct|aggregate|reconcile|needs_decision|unavailable",'
    '"source":"Sheet!B10|null","also_sources":["Sheet!B12"],"coverage_sources":[],'
    '"assumption":"...|null",'
    '"rollup":"sum|end|avg","scenario":"actual|budget|forecast|null","source_unit":"...|null",'
    '"target_unit":"...|null","sign_flip":false,"sign_basis":"...|null",'
    '"period_map":"calendar|positional","confidence":0.0,"note":"..."}]}\n'
    "PERIODS: report every time column/row you can see, whichever AXIS they run on "
    "(down rows or across columns) — header_cell is the cell holding the period header. "
    "SERIES: every data row/column with a label; label_cell is where its label sits.\n"
    "MAPPINGS: reference source series ONLY by 'Sheet!<label_cell>' exactly as you "
    "listed them in sheets[].series — never invent ids, never cite value cells.\n"
    "coverage_sources: when `source` does NOT cover every demanded period (earlier "
    "history years, or a budget/forecast year on another sheet), list the OTHER "
    "'Sheet!<label_cell>' series that mean the SAME metric for the missing periods — "
    "even when their label differs (e.g. 'Total revenue (allocated)' on a history sheet "
    "vs the template's 'Revenue'). NOT also_sources (which SUMS components on one sheet); "
    "coverage_sources are alternative sources of the SAME line for other periods. [] when "
    "`source` already covers everything."
)
_ONEPASS_SYSTEM = _SYSTEM.rsplit("Return ONLY JSON", 1)[0] + _ONEPASS_CONTRACT
_ONEPASS_MAX_TOKENS = 32000


def _stream_thinking_fallback(**kw):
    """guarded_stream, retrying ONCE with thinking OFF when the reply truncates
    at max_tokens — adaptive thinking shares the output budget, and a large
    structured answer (a transposed pack's 90 series + 66 mappings) can be
    squeezed out by its own reasoning. Deterministic decoding gets the whole
    budget on the retry; a still-truncated reply raises for the caller's
    fallback ladder."""
    try:
        return guarded_stream(**kw)
    except RuntimeError as e:
        if "truncated at max_tokens" not in str(e):
            raise
        logger.warning("%s — retrying with thinking off (full budget to output)",
                       str(e)[:120])
        # no temperature: Opus 4.8 rejects the param outright (API 400)
        return guarded_stream(**{**kw, "thinking": False,
                                 "site": f"{kw.get('site', 'llm')}_nothink"})

_SRC_REF = re.compile(r"^(.*)!\s*\$?([A-Za-z]{1,3})\$?(\d+)$")


def understand_and_map(metrics: list[dict], grids: str, context: str = "",
                       images: list[tuple[str, bytes]] | None = None
                       ) -> tuple[list[dict], list[dict], bool]:
    """ONE strong-model call: read the source structure AND map, with both
    workbooks (grids + optional sheet images) in context. Returns
    (sheet_claims, raw_mappings, degraded) — raw mappings reference label
    CELLS and are translated to catalogue ids by ``translate_sources``;
    ``degraded``=True means the structure dump was squeezed (truncation /
    corrective retry) and the caller should prefer RICHER claims from the
    cache or the per-sheet understanding while keeping these mappings (label
    cells resolve against any claims covering the same sheets). Raises on an
    unusable reply; the caller falls back to the two-pass path, loudly."""
    stable = _grid_stable_prefix("(you will define the series yourself — see the "
                                 "output contract)", context, grids)
    var = _grid_metrics_text(metrics)
    blocks: list[dict] = []
    n_images = 0
    if images:
        for cap, png in images:
            if cap:
                blocks.append({"type": "text", "text": cap})
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": __import__("base64").standard_b64encode(png).decode("ascii")}})
        n_images = len(images)
    blocks.append({"type": "text", "text": stable})
    blocks.append({"type": "text", "text": var})
    content: list[dict] = blocks
    cache_blocks = len(blocks) - 1        # everything except the metrics text
    user_text = stable + var

    def _parse_onepass(text: str) -> tuple[list[dict], list[dict]]:
        t = text.strip()
        if t.startswith("```"):
            t = t.split("```", 2)[1]
            if t.lstrip().startswith("json"):
                t = t.lstrip()[4:]
        a, b = t.find("{"), t.rfind("}")
        if a == -1 or b == -1:
            raise ValueError("no JSON object in reply")
        obj = json.loads(t[a:b + 1])
        sheets = [{"sheet": s.get("sheet"), "periods": s.get("periods") or [],
                   "series": s.get("series") or []} for s in obj.get("sheets") or []]
        maps = obj.get("mappings") or []
        if not sheets or not maps:
            raise ValueError("reply missing sheets or mappings")
        return sheets, maps

    degraded = False   # first call truncated → the structure dump was squeezed;
                       # the caller should prefer richer claims from elsewhere
    try:
        _, text = guarded_stream(
            model=MODEL_SMART, system=_ONEPASS_SYSTEM, content=content,
            max_tokens=_ONEPASS_MAX_TOKENS,
            est_input_chars=len(_ONEPASS_SYSTEM) + len(user_text),
            n_images=n_images, site="grid_onepass", cache_blocks=cache_blocks)
    except RuntimeError as e:
        if "truncated at max_tokens" not in str(e):
            raise
        degraded = True
        logger.warning("%s — retrying with thinking off (full budget to output)", str(e)[:120])
        _, text = guarded_stream(
            model=MODEL_SMART, system=_ONEPASS_SYSTEM, content=content,
            max_tokens=_ONEPASS_MAX_TOKENS, thinking=False,
            est_input_chars=len(_ONEPASS_SYSTEM) + len(user_text) // 6,
            n_images=n_images, site="grid_onepass_nothink",
            cache_blocks=cache_blocks)
    try:
        sheets, maps = _parse_onepass(text)
        return sheets, maps, degraded
    except Exception as e:  # noqa: BLE001 — one corrective retry
        logger.warning("one-pass reply didn't parse (%s) — corrective retry", e)
        messages = [
            {"role": "user", "content": content},
            {"role": "assistant", "content": text[:6000]},
            {"role": "user", "content": (
                f"That reply was not usable ({e}). Return ONLY the JSON object with BOTH "
                '"sheets" and "mappings" for the metrics above — no prose, no fences.')},
        ]
        _, text = _stream_thinking_fallback(
            model=MODEL_SMART, system=_ONEPASS_SYSTEM,
            messages=messages, max_tokens=_ONEPASS_MAX_TOKENS,
            site="grid_onepass_retry")
        sheets, maps = _parse_onepass(text)
        return sheets, maps, True   # corrective retry = degraded-confidence claims


def translate_sources(raw_mappings: list[dict],
                      sid_index: dict[tuple[str, str], str],
                      valid_sids: set[str]) -> tuple[list[MetricMap], list[str]]:
    """Label-cell references → catalogue series ids. Unresolvable components
    are DROPPED with a note; an unresolvable primary keeps the entry visible
    as unavailable-with-note (never a silent loss)."""
    lower_index = {(sh.strip().lower(), cell): sid for (sh, cell), sid in sid_index.items()}

    def resolve(ref) -> str | None:
        s = str(ref or "").strip()
        if not s:
            return None
        if s in valid_sids:            # the model echoed a real id — accept
            return s
        m = _SRC_REF.match(s)
        if not m:
            return None
        return lower_index.get((m.group(1).strip().strip("'").lower(),
                                f"{m.group(2).upper()}{m.group(3)}"))

    out: list[MetricMap] = []
    notes: list[str] = []
    for r in raw_mappings:
        sid = resolve(r.get("source"))
        also: list[str] = []
        for a in r.get("also_sources") or []:
            rsid = resolve(a)
            if rsid and rsid != sid and rsid not in also:
                also.append(rsid)
            elif not rsid:
                notes.append(f"{r.get('metric')}: component ref {a!r} not resolvable — dropped")
        coverage: list[str] = []
        for cref in r.get("coverage_sources") or []:
            rsid = resolve(cref)
            if rsid and rsid != sid and rsid not in coverage:
                coverage.append(rsid)
            elif not rsid:
                notes.append(f"{r.get('metric')}: coverage ref {cref!r} not resolvable — dropped")
        entry = dict(r)
        entry["series_id"] = sid
        entry["also_series_ids"] = also
        entry["coverage_series_ids"] = coverage
        if r.get("source") and sid is None:
            entry["status"] = "unavailable"
            entry["note"] = (f"source ref {r.get('source')!r} did not resolve to a "
                             f"catalogued series. {entry.get('note') or ''}").strip()
            notes.append(f"{r.get('metric')}: primary ref {r.get('source')!r} not resolvable")
        out.append(MetricMap(**entry))
    return out, notes[:40]


def revise_for_checks(failures: list[dict], metric_maps, catalogue: dict[str, Series],
                      grids: str, context: str = "",
                      flags: list[str] | None = None) -> list[MetricMap]:
    """TIE-OUT revision: the template's OWN check formulas failed after the
    fill (EBITDA doesn't tie, cash movement doesn't reconcile). A workbook
    delivered with failing tie-outs is worse than a blank one — so the model
    sees the exact failing checks, the current plan and the grids, and revises
    the mis-mapped/missing/double-counted metrics. One bounded iteration; the
    final render re-evaluates every check, and anything still failing is
    reported loudly and filed for review — never delivered silently."""
    from app.population.cost import SpendCapExceeded

    if not failures or not catalogue:
        return []
    fail_lines = "\n".join(
        f"  {f.get('sheet')}!{f.get('cell')} \"{str(f.get('label'))[:60]}\": "
        f"computes {str(f.get('after'))[:18]} (a passing check reads OK/TRUE/~0)"
        for f in failures[:25])
    plan_lines = "\n".join(
        f"  {m.metric} -> {m.series_id or 'UNFILLED'}"
        + (f" +{m.also_series_ids}" if m.also_series_ids else "")
        + f" ({m.status}, conf {m.confidence:g})"
        for m in metric_maps if m.series_id or m.status != "unavailable")[:12000]
    flag_lines = ("\nKNOWN GAPS (source sheets NOT catalogued — their data has no ids; "
                  "do not invent series for them):\n  " + "\n  ".join(flags)
                  if flags else "")
    stable = _grid_stable_prefix(_series_lines(catalogue), context, grids)
    var = ("\n\nTHE TEMPLATE'S OWN CHECK FORMULAS FAIL after this fill — the totals do "
           "not tie. This means a component is MIS-MAPPED, DOUBLE-COUNTED, MISSING, or "
           "carries the wrong sign/scale. Failing checks:\n" + fail_lines
           + "\n\nCURRENT PLAN:\n" + plan_lines + flag_lines
           + "\n\nLooking at the grids and the check formulas' inputs, revise the "
             "mapping entries responsible. Return entries ONLY for metrics you are "
             "changing; if a failure is caused by data the source genuinely lacks, "
             "change nothing for it and it will be reported for review. Never force "
             "a fill just to make a check pass.\n"
             'Return ONLY the JSON {"mappings":[...]}.')
    content = [{"type": "text", "text": stable}, {"type": "text", "text": var}]
    try:
        _, text = _stream_thinking_fallback(
            model=MODEL_SMART, system=_SYSTEM, content=content,
            est_input_chars=len(_SYSTEM) + len(var) + len(stable) // 4,
            max_tokens=14000, site="tie_out_revision", cache_blocks=1)
        return _parse(text)
    except SpendCapExceeded:
        raise
    except Exception as e:  # noqa: BLE001 — the failure still reports loudly downstream
        logger.warning("tie-out revision failed (%s) — check failures stand and report", e)
        return []


def revise_plan(problems: list[dict], catalogue: dict[str, Series],
                grids: str, context: str = "") -> list[MetricMap]:
    """OUTCOME-driven revision: the model sees what actually happened to the
    problem metrics (its own entry + the executor's per-metric reasons) with
    the grids still in context, and returns revised entries for ONLY the
    metrics it believes it can genuinely improve — or none. Best-effort."""
    from app.population.cost import SpendCapExceeded

    if not problems or not catalogue:
        return []
    blocks = []
    for p in problems:
        blocks.append(f"METRIC: {p['metric']}\nYOUR PLAN: {json.dumps(p['plan'])}\n"
                      f"OUTCOME: {p['outcome']}")
    stable = _grid_stable_prefix(_series_lines(catalogue), context, grids)
    var = ("\n\nThese metrics did NOT fully fill. For each, either return a REVISED "
           "mapping entry (only if, looking at the grids, you can see a genuinely "
           "better answer) or omit it (the current outcome stands). Never force a "
           "bad fill to make a blank go away.\n\n"
           + "\n\n".join(blocks)
           + "\n\nReturn ONLY the JSON {\"mappings\":[...]} for metrics you are revising.")
    content = [{"type": "text", "text": stable}, {"type": "text", "text": var}]
    try:
        _, text = _stream_thinking_fallback(
            model=MODEL_SMART, system=_SYSTEM, content=content,
            est_input_chars=len(_SYSTEM) + len(var) + len(stable) // 4,
            max_tokens=14000, site="plan_revision", cache_blocks=1)
        return _parse(text)
    except SpendCapExceeded:
        raise
    except Exception as e:  # noqa: BLE001 — revision is best-effort by design
        logger.warning("revision pass failed (%s) — first outcome stands", e)
        return []


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


def _grid_stable_prefix(series_block: str, context: str, grids: str) -> str:
    """The CACHEABLE part of a grid-mode call: context + grids + catalogue —
    byte-identical across mapping batches, the repair round and the revision
    pass (all share _SYSTEM), so every call after the first pays ~10% for it.
    The per-call metrics/problem text goes in a separate block AFTER this."""
    ctx = f"TEMPLATE CONTEXT (authoritative — sponsor-confirmed):\n{context}\n\n" if context else ""
    return (f"{ctx}{_GRID_PREAMBLE}{grids}\n\n"
            "SOURCE SERIES (id | sheet | label [unit] | samples):\n"
            f"{series_block}")


def _grid_metrics_text(metrics: list[dict]) -> str:
    return ("\n\nTEMPLATE METRICS to map (key | label | unit | def | qualifies):\n"
            f"{_metric_lines(metrics)}\n\nReturn the JSON now.")


def _user_text(metrics: list[dict], series_block: str, context: str = "",
               grids: str | None = None) -> str:
    ctx = f"TEMPLATE CONTEXT (authoritative — sponsor-confirmed):\n{context}\n\n" if context else ""
    grid_block = f"{_GRID_PREAMBLE}{grids}\n\n" if grids else ""
    return (
        f"{ctx}{grid_block}"
        "SOURCE SERIES (id | sheet | label [unit] | samples):\n"
        f"{series_block}\n\n"
        "TEMPLATE METRICS to map (key | label | unit | def | qualifies):\n"
        f"{_metric_lines(metrics)}\n\n"
        "Return the JSON now."
    )


def estimate_mapping_usd(metrics: list[dict], catalogue: dict[str, Series],
                         max_tokens: int = 8000, context: str = "",
                         grids: str | None = None) -> float:
    """Dry-run cost: what the whole mapping step would cost before sending anything."""
    series_block = _series_lines(catalogue)
    total = 0.0
    if grids:
        stable = _grid_stable_prefix(series_block, context, grids)
        tier = _grid_tier_model(len(metrics))
        for n, i in enumerate(range(0, len(metrics), _GRID_HALF)):
            chunk = metrics[i:i + _GRID_HALF]
            var_chars = len(_grid_metrics_text(chunk))
            chars = len(_SYSTEM) + var_chars + (len(stable) if n == 0 else len(stable) // 8)
            total += estimate_call_usd(tier, chars, _GRID_MAX_TOKENS)
        return round(total, 4)
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


def _grid_tier_model(n_metrics: int) -> str:
    """Which model maps in grid mode: Opus for normal templates, Sonnet for
    huge ones. Opus writes output at 5x Sonnet's price and ~1/3 its speed, and
    a 231-metric template's mapping cost is ~90% OUTPUT tokens — the grids
    (the actual quality lever) are identical either way, and the tie-out loop
    + eval net guard quality. TEMPO_OPUS_METRIC_LIMIT tunes the threshold."""
    try:
        limit = int(os.environ.get("TEMPO_OPUS_METRIC_LIMIT", "100"))
    except ValueError:
        limit = 100
    return MODEL_SMART if n_metrics <= limit else MODEL_MAP


def _map_chunk(chunk: list[dict], series_block: str, max_tokens: int,
               context: str = "", grids: str | None = None,
               cache_hit: bool = False,
               model_override: str | None = None) -> list[MetricMap]:
    """One mapping batch, with ONE corrective retry when the reply doesn't parse
    (or parses to nothing for a non-empty chunk). Raises after the retry fails —
    the caller decides whether that kills the run. In grid mode the call runs
    on the STRONG model, the grids ride in a CACHED prefix block (``cache_hit``
    marks calls whose prefix is already cached, so the guard estimates them at
    the cached rate instead of aborting a run for spend it won't incur)."""
    if grids:
        stable = _grid_stable_prefix(series_block, context, grids)
        var = _grid_metrics_text(chunk)
        content: list[dict] | str = [{"type": "text", "text": stable},
                                     {"type": "text", "text": var}]
        est = len(_SYSTEM) + len(var) + (len(stable) // 8 if cache_hit else len(stable))
        model, cache_blocks = (model_override or MODEL_SMART), 1
        temperature = None            # Opus 4.8 rejects the param
    else:
        content = _user_text(chunk, series_block, context)
        est = len(_SYSTEM) + len(content)
        model, cache_blocks = MODEL_MAP, 0
        temperature = 0
    user = content
    _, text = guarded_stream(model=model, system=_SYSTEM, content=content,
                             max_tokens=max_tokens, est_input_chars=est,
                             temperature=temperature, site="metric_planner",
                             cache_blocks=cache_blocks)
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
    _, text = guarded_stream(model=model, system=_SYSTEM, messages=messages,
                             max_tokens=max_tokens,
                             est_input_chars=(est // 4 if grids else None),
                             temperature=temperature, site="metric_planner_retry",
                             cache_blocks=cache_blocks)
    maps = _parse(text)
    if not maps:
        raise RuntimeError("mapping batch unusable after corrective retry")
    return maps


def repair_plan(failing: list[tuple[dict, "MetricMap", list]], catalogue: dict[str, Series],
                max_tokens: int = 8000, context: str = "",
                grids: str | None = None) -> list[MetricMap]:
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
    var = ("\n\nThe deterministic verifier could not execute these plan entries. "
           "Revise EACH one to something executable against the facts above (or mark it "
           "needs_decision/unavailable with a clear reason — never force a bad fill):\n\n"
           + "\n\n".join(blocks)
           + "\n\nReturn the corrected JSON now (mappings for ONLY these metrics).")
    if grids:
        stable = _grid_stable_prefix(_series_lines(catalogue), context, grids)
        content = [{"type": "text", "text": stable}, {"type": "text", "text": var}]
        est = len(_SYSTEM) + len(var) + len(stable) // 4   # prefix usually cached already
        model, cache_blocks, temperature = MODEL_SMART, 1, None
    else:
        content = (
            (f"TEMPLATE CONTEXT (authoritative — sponsor-confirmed):\n{context}\n\n" if context else "")
            + "SOURCE SERIES (id | sheet | label [unit] | periods | samples):\n"
            + _series_lines(catalogue) + var)
        est = len(_SYSTEM) + len(content)
        model, cache_blocks, temperature = MODEL_MAP, 0, 0
    try:
        _, text = guarded_stream(model=model, system=_SYSTEM, content=content,
                                 est_input_chars=est, max_tokens=max_tokens,
                                 temperature=temperature, site="plan_repair",
                                 cache_blocks=cache_blocks)
        return _parse(text)
    except SpendCapExceeded:
        raise
    except Exception as e:  # noqa: BLE001 — repair is best-effort by design
        logger.warning("plan repair round failed (%s) — issues fall through to questions", e)
        return []


def map_metrics(metrics: list[dict], catalogue: dict[str, Series],
                max_tokens: int = 8000, context: str = "",
                grids: str | None = None) -> tuple[list[MetricMap], list[str]]:
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

    if grids:
        # GRID MODE: batch 1 runs alone (writes the prompt cache for the
        # shared grids prefix), the rest run CONCURRENTLY as cache hits —
        # a large template's mapping drops from N sequential full-price Opus
        # calls (a real run burned $20 / 30 min re-sending identical grids)
        # to one full-price call plus N-1 cheap parallel ones.
        chunks = [metrics[i:i + _GRID_HALF] for i in range(0, len(metrics), _GRID_HALF)]
        tier = _grid_tier_model(len(metrics))
        try:
            out.extend(_map_chunk(chunks[0], series_block, _GRID_MAX_TOKENS,
                                  context, grids, cache_hit=False,
                                  model_override=tier))
        except SpendCapExceeded:
            raise
        except Exception:  # noqa: BLE001
            failed_metrics.extend(str(m.get("metric")) for m in chunks[0])
            logger.exception("grid mapping batch 1 failed after retry")
        if len(chunks) > 1:
            from concurrent.futures import ThreadPoolExecutor

            from app.llm import bind_worker, get_llm_context
            from app.population.cost import get_guard

            def _run(chunk):
                return _map_chunk(chunk, series_block, _GRID_MAX_TOKENS,
                                  context, grids, cache_hit=True,
                                  model_override=tier)

            with ThreadPoolExecutor(max_workers=4, initializer=bind_worker,
                                    initargs=(get_guard(), get_llm_context())) as pool:
                futs = {pool.submit(_run, c): c for c in chunks[1:]}
                for fut, chunk in futs.items():
                    try:
                        out.extend(fut.result())
                    except SpendCapExceeded:
                        raise
                    except Exception:  # noqa: BLE001
                        failed_metrics.extend(str(m.get("metric")) for m in chunk)
                        logger.exception("grid mapping batch failed after retry")
        return out, failed_metrics

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
