"""Assemble per-sheet prompt context from the snapshot.

Pure builders (annotations, workbook context, deterministic hints) shared by
the workbook orchestrator. The single-sheet entry point that used to live here
was removed: it had no callers and never armed a SpendGuard, making it the one
uncapped Opus+vision path — workbook.understand_workbook is the only way to
run the per-sheet agent, and it arms the guard first.
"""

from __future__ import annotations

_CAP = 40  # cap list lengths fed to the prompt


def _annotations(sheet: dict) -> str:
    lines: list[str] = []
    for t in sheet.get("text_box_notes", [])[:_CAP]:
        near = ", ".join(t.get("nearby_labels", [])[:4])
        lines.append(
            f"- TextBox @{t.get('coverage_range') or t.get('anchor_cell')}: "
            f"\"{(t.get('text') or '').strip()[:300]}\""
            + (f"  (near: {near})" if near else "")
        )
    for v in sheet.get("data_validations", [])[:_CAP]:
        allowed = v.get("allowed_values") or []
        av = f" allowed={allowed[:12]}" if allowed else ""
        prompt = (v.get("prompt_message") or "").strip()
        pm = f" prompt=\"{prompt[:120]}\"" if prompt else ""
        lines.append(f"- Validation {v.get('cell_range')} type={v.get('validation_type')}{av}{pm}")
    for c in sheet.get("comments", [])[:_CAP]:
        lines.append(f"- Comment @{c.get('cell_address')}: \"{(c.get('text') or '').strip()[:200]}\"")
    return "\n".join(lines)


def _workbook_ctx(snap: dict) -> str:
    names = [s["name"] for s in snap.get("sheets", [])]
    nr = snap.get("named_ranges", [])
    nr_lines = [f"{n['name']} -> {', '.join(n.get('destinations', []))}" for n in nr[:60]]
    return (
        f"All sheets ({len(names)}): {names}\n"
        f"Named ranges ({len(nr)}): " + "; ".join(nr_lines) + "\n"
        "Reporting/as-of date: unknown (not yet extracted)"
    )


def _cross_sheet_counts(snap: dict, sheet_name: str) -> tuple[dict, dict]:
    """(reads_from, read_by): how this sheet's formulas reference other sheets,
    and how other sheets' formulas reference this one — from the dep graph."""
    reads_from: dict[str, int] = {}
    read_by: dict[str, int] = {}
    for s in snap.get("sheets", []):
        src = s["name"]
        for c in s.get("cells", []):
            for rng in c.get("precedents", []):
                i = rng.rfind("!")
                if i == -1:
                    continue
                ref = rng[:i].strip("'")
                if src == sheet_name and ref != sheet_name:
                    reads_from[ref] = reads_from.get(ref, 0) + 1
                elif ref == sheet_name and src != sheet_name:
                    read_by[src] = read_by.get(src, 0) + 1
    return reads_from, read_by


def _hints(snap: dict, sheet_name: str) -> str:
    g = snap.get("formula_graph", {})
    prefix = f"{sheet_name}!"
    inputs = sorted(a[len(prefix):] for a in g.get("input_cells", []) if a.startswith(prefix))
    nr_here = [
        n["name"]
        for n in snap.get("named_ranges", [])
        if any(prefix in d or f"'{sheet_name}'!" in d for d in n.get("destinations", []))
    ]
    sheet = next((s for s in snap["sheets"] if s["name"] == sheet_name), {})
    reads_from, read_by = _cross_sheet_counts(snap, sheet_name)
    top = lambda d: dict(sorted(d.items(), key=lambda kv: -kv[1])[:8])
    # PUSH topology: cells a CX_PUSH formula READS are entry cells by the
    # workbook's own declaration — and the push formulas usually sit beyond the
    # grid's column cap, so this hint is how the model learns of them.
    push_line = ""
    try:
        from app.datamodel.topology import push_entry_summary
        cf = {(sheet_name, c["row"], c["col"]): c["formula"]
              for c in sheet.get("cells", []) if c.get("formula")}
        ps = push_entry_summary(cf, sheet_name)
        if ps:
            push_line = (f"\nPUSH-ENTRY cells (a CX_PUSH formula pushes what is typed "
                         f"there — user data-entry area): {ps}")
    except Exception:  # noqa: BLE001 — hints are best-effort
        pass
    return (
        f"formula-graph input cells on this sheet ({len(inputs)}): "
        f"{inputs[:80]}{' …' if len(inputs) > 80 else ''}\n"
        f"named ranges on this sheet: {nr_here[:40]}\n"
        f"detected regions on this sheet: {len(sheet.get('regions', []))}\n"
        f"cross-sheet — this sheet READS FROM: {top(reads_from)}\n"
        f"cross-sheet — this sheet is READ BY: {top(read_by)}" + push_line
    )
