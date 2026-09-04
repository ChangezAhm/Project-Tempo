"""Reporting: the final apply, every honesty channel (review notes, coverage,
unused series, sign checks, unmatched reasons) and every batched review
question the run files. Nothing here changes the fill — it explains it."""

from __future__ import annotations

import logging
from collections import Counter

from app.population.apply import apply_links
from app.population.pipeline.state import RunState
from app.population.schema import metric_key

logger = logging.getLogger(__name__)


def stage_report(state: RunState) -> None:
    _collect_link_review_notes(state)
    _file_fiscal_question(state)
    state.notes = [m.note for m in state.metric_maps if m.series_id and m.note][:200]
    # WHY the uncovered metrics are uncovered, in the mapper's own words: a null
    # mapping carries a reason ('source combines depreciation & amortisation',
    # 'source splits opex by function, template wants it by nature') so a blank
    # is explained to the user instead of mysterious.
    state.coverage_notes = [m.note for m in state.metric_maps
                           if not m.series_id and m.note][:100]
    _collect_unused_series(state)
    state.result = apply_links(state.target_inputs, state.source_snapshot,
                               state.links, skipped=[])
    _check_sign_violations(state)
    _upgrade_unmatched_reasons(state)
    _file_plan_and_outcome_questions(state)


def _collect_link_review_notes(state: RunState) -> None:
    """Filled cells that need a human eye: scale that couldn't be magnitude-
    verified, a reconciled fill, positional alignment, or a flagged
    auto-resolution."""
    state.review = [{"template_sheet": lk.template_sheet, "template_cell": lk.template_cell,
                     "note": lk.note}
                    for lk in state.links if lk.note and any(t in lk.note for t in
                                                             ("unverified", "reconciled", "positional",
                                                              "auto-resolved", "fiscal:"))]


def _file_fiscal_question(state: RunState) -> None:
    """ONE batched question for the fiscal-convention flag: proceeding on the
    calendar reading without asking would be a silent plausible-wrong."""
    fiscal_notes = [lk for lk in state.links if lk.note and "fiscal:" in lk.note]
    if not fiscal_notes:
        return
    try:
        from app.review.items import make_item
        cells_f = sorted({f"{lk.template_sheet}!{lk.template_cell}" for lk in fiscal_notes})
        state.ask([make_item(
            source="populate-plan", kind="judgment",
            question=(f"This source reports a non-calendar fiscal year, and {len(cells_f)} "
                      "FY-column fill(s) were built on the CALENDAR-year reading — should "
                      "the template's FY columns follow the company's fiscal year instead?"),
            why=("Affected e.g. " + ", ".join(cells_f[:6])
                 + ". 'FY25' can mean fiscal FY25 or calendar 2025; the sponsor's "
                   "convention decides, not the system."),
            suggested_answer="calendar year is correct — keep as filled",
        )], priority=2)
    except Exception as e:  # noqa: BLE001 — inbox filing must never fail the run
        logger.warning("could not file fiscal-convention question: %s", e)


def _collect_unused_series(state: RunState) -> None:
    """Never silently drop source data: any source series no mapping used at all —
    this is how UNDER-counting (e.g. G&A left out of a reconciled opex line)
    surfaces instead of hiding. A real-but-unused cost line is a red flag.
    USED = produced at least one real link. A series claimed by a mapping
    that never executed (e.g. a derived-math reconcile the executor can't
    run) is still AVAILABLE — counting it used once masked the entire
    revenue block sitting empty while 'Total turnover' fed an unexecutable
    growth metric."""
    filled_metric_keys = {metric_key(f) for f in state.target_inputs
                          if (f.get("sheet_name"), (f.get("cell") or "").upper())
                          in {(lk.template_sheet, (lk.template_cell or "").upper())
                              for lk in state.links}}
    used_series = ({m.series_id for m in state.metric_maps
                    if m.series_id and m.metric in filled_metric_keys}
                   | {sid for m in state.metric_maps if m.metric in filled_metric_keys
                      for sid in (m.also_series_ids or [])})
    state.unused_source_series = sorted(
        s.label for sid, s in state.catalogue.items() if sid not in used_series)
    # No silent narrowing of COVERAGE either: name the source sheets the
    # densest-N selection never sent to understanding — a sparse but critical
    # sheet must be visible, not invisibly dropped.
    try:
        from app.population.source_understanding import select_sheets
        seen_sheets = {s.get("name") for s in select_sheets(state.source_snapshot)}
        skipped_src = [s.get("name") for s in state.source_snapshot.get("sheets", [])
                       if s.get("name") not in seen_sheets]
        if skipped_src:
            state.routing["source_sheets_skipped"] = skipped_src[:20]
    except Exception:  # noqa: BLE001 — accounting only
        pass


def _check_sign_violations(state: RunState) -> None:
    """Rule enforcement: check every written value against the template's own
    declared sign convention. Violations stay written (the reviewer decides)
    but are flagged here AND filed as durable review items in the inbox."""
    from app.population.checks import sign_violations
    state.violations = sign_violations(state.result.filled, state.target_inputs)
    for v in state.violations:
        state.review.append({"template_sheet": v["template_sheet"],
                             "template_cell": v["template_cell"],
                             "note": f"sign violation: expected {v['expected']} ({v['rule'][:60]})"})
    if state.violations:
        from app.review.items import make_item
        cells = [f"{v['template_sheet']}!{v['template_cell']}" for v in state.violations]
        state.ask([make_item(
            source="populate", kind="judgment",
            question=(f"{len(cells)} fill(s) landed with a sign against the template's stated "
                      f"convention ({', '.join(cells[:5])}{'…' if len(cells) > 5 else ''}) — "
                      "keep the values as sourced?"),
            why="; ".join(f"{v['template_sheet']}!{v['template_cell']} ({v['metric']}): "
                          f"expected {v['expected']}" for v in state.violations[:10]),
            affected={"cells": cells[:20]},
            suggested_answer="yes — keep as sourced",
        )], priority=4)


def _upgrade_unmatched_reasons(state: RunState) -> None:
    """Upgrade apply_links' generic "no source match" to the executor's precise
    reason (low confidence / no source period / unit unresolved / currency
    mismatch), then the reason histogram + named unmapped metrics — the
    headline of WHY cells are blank belongs in the response, not buried in a
    1,000-row audit list."""
    reasons = {(u.get("template_sheet"), (u.get("template_cell") or "").upper()): u.get("reason")
               for u in state.bind_unmatched}
    for u in state.result.unmatched:
        k = (u.get("template_sheet"), (u.get("template_cell") or "").upper())
        if u.get("reason") == "no source match" and k in reasons:
            u["reason"] = reasons[k]
    state.unmatched_reasons = [{"reason": r, "count": n}
                               for r, n in Counter(u.get("reason")
                                                   for u in state.result.unmatched).most_common(10)]
    # '298x no source series mapped' reads as failure when most of it is the
    # source honestly not containing those metrics — NAME them so the user
    # can see at a glance what this source doesn't cover.
    state.label_by_key = {m["metric"]: (m.get("label") or m["metric"])
                          for m in state.demand["metrics"]}
    state.unmapped_metrics = sorted({state.label_by_key.get(u.get("metric"), str(u.get("metric")))
                                     for u in state.result.unmatched
                                     if str(u.get("reason", "")).startswith("no source series")})


def _file_plan_and_outcome_questions(state: RunState) -> None:
    label_by_key = state.label_by_key
    # RECONCILIATIONS: metrics the source carries at a different granularity, filled
    # provisionally with an explicit assumption. Surface them, and file each as a
    # durable review question so the user confirms/corrects ONCE — the answer then
    # feeds every future run via the mapping context channel (load_context).
    state.reconciled_metrics = [{"metric": label_by_key.get(m.metric, m.metric),
                                 "assumption": m.assumption or ""}
                                for m in state.metric_maps if m.status == "reconcile"]
    # needs_decision: the source has related data but assigning it needs a human
    # choice we must not guess — asked (never filled), so the user decides once.
    state.open_questions = [{"metric": label_by_key.get(m.metric, m.metric),
                             "question": m.assumption or m.note or ""}
                            for m in state.metric_maps if m.status == "needs_decision"]
    if state.reconciled_metrics or state.open_questions:
        try:
            from app.review.items import make_item
            items = []
            if state.reconciled_metrics:
                # ONE tap, not N: the reconciles are one judgment call ("fill
                # close matches rather than leave blanks"); per-metric detail
                # lives in `why`, granular remaps stay possible on the
                # contract page.
                names = [r["metric"] for r in state.reconciled_metrics]
                listed = ", ".join(names[:6]) + ("…" if len(names) > 6 else "")
                items.append(make_item(
                    source="populate", kind="judgment",
                    question=(f"{len(names)} line(s) filled from closest matches "
                              f"({listed}) — keep them?"),
                    why=chr(10).join(f"{r['metric']}: {r['assumption']}"
                                     for r in state.reconciled_metrics),
                    affected={"metrics": names},
                    suggested_answer="yes — keep them",
                ))
            items += [make_item(
                source="populate", kind="judgment",
                question=f"{q['metric']}: {(q['question'] or 'needs your mapping decision').rstrip('.')}?",
                why="The source has related data but the mapping needs your decision; left blank until you choose.",
                affected={"metrics": [q["metric"]]},
                suggested_answer="leave it blank",
            ) for q in state.open_questions]
            state.ask(items, priority=1)
        except Exception as e:  # noqa: BLE001 — inbox filing must never fail the run
            logger.warning("could not build reconciliation/decision questions: %s", e)

    # FILL-PLAN questions: every question-tier verifier/executor issue becomes
    # ONE batched review item per (metric, issue) with the plan's proposal as
    # the one-tap answer; the answer persists as a contract decision and
    # replays on every future run. Flagged auto-resolutions land in `review`.
    if state.plan_issues:
        from app.population.contract import decision_spec
        from app.review.items import make_item
        _DEC_FIELD = {"LOW_CONFIDENCE": "confirmed", "GRAIN_UNBRIDGEABLE": "rollup",
                      "SCALE_CONFLICT": "source_unit", "BUCKET_INCOMPLETE": "rollup"}
        plan_q_items = []
        # SCENARIO_NO_SOURCE is one underlying fact (this source has no
        # budget/forecast data), not N per-metric decisions — ONE question.
        scen_gaps = [i for i in state.plan_issues if i.code == "SCENARIO_NO_SOURCE"
                     and i.severity == "question"]
        if scen_gaps:
            metrics = sorted({label_by_key.get(i.metric, i.metric) for i in scen_gaps})
            scens = sorted({i.scenario for i in scen_gaps if i.scenario})
            # Tell the TRUTH about what the source carries: when a sibling
            # scenario's columns exist (the classic budget-vs-forecast naming
            # split), the question is a one-tap equivalence DECISION the
            # executor replays — not a "no data" shrug that reads as a miss.
            src_tags = {t for s in state.catalogue.values()
                        for t in (s.col_scenario or {}).values()} \
                | {s.scenario for s in state.catalogue.values() if s.scenario} \
                | {v for s in state.catalogue.values() for v in (s.variants or {})}
            _SIBLING = {"budget": "forecast", "forecast": "budget"}
            subs = {sc: _SIBLING[sc] for sc in scens
                    if sc in _SIBLING and _SIBLING[sc] in src_tags and sc not in src_tags}
            if subs:
                pairs = ", ".join(f"{k} ⇐ {v}" for k, v in subs.items())
                plan_q_items.append(make_item(
                    source="populate-plan", kind="judgment",
                    question=(f"The template's {'/'.join(subs)} columns have no "
                              f"{'/'.join(subs)}-tagged source data, but the source carries "
                              f"{'/'.join(sorted(set(subs.values())))} data for those periods — "
                              f"fill them from it ({pairs})?"),
                    why=("Affected: " + ", ".join(metrics[:12])
                         + ("…" if len(metrics) > 12 else "")
                         + ". Sources and templates often name the same months differently; "
                           "answering yes stores the equivalence and replays it every run."),
                    suggested_answer=("yes — use " + "/".join(sorted(set(subs.values())))
                                      + " for those columns"),
                    check_spec=decision_spec("*", "scenario_equivalence", subs,
                                             scope="template"),
                ))
                remaining = [sc for sc in scens if sc not in subs]
            else:
                remaining = scens
            if remaining:
                plan_q_items.append(make_item(
                    source="populate-plan", kind="judgment",
                    question=(f"The template has {'/'.join(remaining)} columns for "
                              f"{len(metrics)} metric(s) but this source carries no "
                              f"{'/'.join(remaining)} data — those slots stay blank. OK?"),
                    why="Affected: " + ", ".join(metrics[:12]) + ("…" if len(metrics) > 12 else ""),
                    suggested_answer="yes — leave them blank",
                ))
        for i in state.plan_issues:
            if i.code == "SCENARIO_NO_SOURCE":
                continue
            label = label_by_key.get(i.metric, i.metric)
            if i.severity == "default" and i.resolution:
                state.review.append({"template_sheet": (i.cells[0].split("!", 1)[0] if i.cells else None),
                                     "template_cell": (i.cells[0].split("!", 1)[1] if i.cells else None),
                                     "note": f"auto-resolved [{i.code}] '{label}': {i.resolution}"})
                continue
            if i.severity != "question":
                continue
            field = _DEC_FIELD.get(i.code)
            # unit answers belong to the SOURCE FAMILY ("this pack is in
            # '000s"), not the template — scoped so an unrelated file never
            # inherits them; everything else is template-scoped.
            scope = "source_format" if field == "source_unit" else "template"
            spec = (decision_spec(i.metric, field, i.suggested_resolution,
                                  scope=scope, fingerprint=state.src_fp) if field else None)
            why = f"[{i.code}] " + (f"Affects {len(i.cells)} cell(s), e.g. "
                                    f"{', '.join(i.cells[:4])}." if i.cells else "")
            # source_format questions carry the family fingerprint in their
            # item_key source — answering source A's unit question must not
            # suppress the SAME question for source family B.
            item_src = (f"populate-plan:{state.src_fp[:8]}" if scope == "source_format"
                        else "populate-plan")
            plan_q_items.append(make_item(
                source=item_src, kind="judgment",
                question=f"{label}: use the suggested answer?",
                why=f"{i.detail}. {why}",
                affected={"metrics": [i.metric], "cells": i.cells[:12]},
                suggested_answer=i.suggested_resolution, check_spec=spec))
        if plan_q_items:
            state.ask(plan_q_items, priority=3)
            state.routing["plan_questions_filed"] = len(plan_q_items)

    # UNDER-COUNTING check as a question, not a dead list: source series no
    # mapping touched. One batched informational item (content-addressed, so
    # answering once suppresses it for good).
    if state.unused_source_series:
        try:
            from app.review.items import make_item
            names = ", ".join(state.unused_source_series[:15]) \
                + ("…" if len(state.unused_source_series) > 15 else "")
            state.ask([make_item(
                source="populate-plan", kind="judgment",
                question=(f"{len(state.unused_source_series)} source series went unused "
                          f"({names}) — is that expected?"),
                suggested_answer="yes — nothing missing",
                why="Unused source data can mean under-counting (a cost line left out of a reconciled total).",
            )], priority=7)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not file unused-series item: %s", e)
