"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  answerReviewItem,
  detectRegions,
  getRegions,
  getReviewItems,
  getUnderstanding,
  parseTemplate,
  populateTemplate,
  understandTemplate,
  verifyReviewItem,
  type ExtensibleRegion,
  type PopulateResult,
  type Understanding,
} from "@/lib/templates";
import {
  BackLink,
  Badge,
  Button,
  EmptyState,
  ErrorNote,
  LinkButton,
  Panel,
  SectionHeader,
  cx,
} from "@/app/components/ui";
import { AnatomyView, OnboardingLive } from "./anatomy";

/* --- Populate --------------------------------------------------------------- */

function PopulatePanel({ templateId }: { templateId: string }) {
  const [asOf, setAsOf] = useState("");
  const [reset, setReset] = useState<"values" | "full">("values");
  const [addLines, setAddLines] = useState<"off" | "propose" | "apply">("propose");
  const [dragging, setDragging] = useState(false);
  const [running, setRunning] = useState(false);
  const [fileName, setFileName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<PopulateResult | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const VALID = /\.(xlsx|xlsm|xls)$/i;

  async function handleFile(file: File) {
    if (!VALID.test(file.name)) {
      setError("Drop an Excel file (.xlsx, .xlsm, .xls).");
      return;
    }
    setRunning(true);
    setError(null);
    setResult(null);
    setFileName(file.name);
    try {
      setResult(await populateTemplate(templateId, file, { asOf: asOf || null, reset, addLines }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Population failed");
    } finally {
      setRunning(false);
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    if (running) return;
    const file = e.dataTransfer.files?.[0];
    if (file) void handleFile(file);
  }

  return (
    <Panel className="p-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <SectionHeader
          title="Populate"
          hint="Drop a portfolio company's data file — Tempo fills this template's inputs."
        />
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-neutral-500">
            As-of date
            <input
              type="date"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
              className="mt-1 block rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-sm"
            />
          </label>
          <label className="text-xs text-neutral-500">
            Refresh
            <select
              value={reset}
              onChange={(e) => setReset(e.target.value as "values" | "full")}
              className="mt-1 block rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-sm"
            >
              <option value="values">Standard clear</option>
              <option value="full">Full reset</option>
            </select>
          </label>
          <label className="text-xs text-neutral-500">
            New line items
            <select
              value={addLines}
              onChange={(e) => setAddLines(e.target.value as "off" | "propose" | "apply")}
              className="mt-1 block rounded-md border border-neutral-300 bg-white px-2 py-1.5 text-sm"
            >
              <option value="propose">Propose only</option>
              <option value="apply">Apply</option>
              <option value="off">Off</option>
            </select>
          </label>
        </div>
      </div>

      <div
        role="button"
        tabIndex={0}
        onClick={() => !running && inputRef.current?.click()}
        onKeyDown={(e) => {
          if ((e.key === "Enter" || e.key === " ") && !running) inputRef.current?.click();
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (!running) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cx(
          "mt-4 flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center transition",
          dragging
            ? "border-ink bg-neutral-50"
            : "border-neutral-300 hover:border-neutral-400 hover:bg-neutral-50/60",
          running && "pointer-events-none opacity-60"
        )}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".xlsx,.xlsm,.xls"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void handleFile(file);
            e.target.value = "";
          }}
        />
        {running ? (
          <>
            <p className="text-sm font-medium text-neutral-700">Populating from {fileName}…</p>
            <p className="mt-1 text-xs text-neutral-500">
              Matching the file to this template — a few minutes.
            </p>
          </>
        ) : (
          <>
            <p className="text-sm font-medium text-neutral-700">Drop an Excel file here</p>
            <p className="mt-1 text-xs text-neutral-400">or click to choose · .xlsx, .xlsm, .xls</p>
          </>
        )}
      </div>

      {error ? <div className="mt-3"><ErrorNote>{error}</ErrorNote></div> : null}

      {result ? (
        <div className="mt-4 space-y-3">
          {result.routing?.hint ? (
            <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-700">{result.routing.hint}</p>
          ) : null}
          {result.additions_applied?.length ? (
            <p className="rounded-md bg-role-input-soft px-3 py-2 text-sm text-role-input">
              {result.additions_applied.length} new line item{result.additions_applied.length === 1 ? "" : "s"} written:{" "}
              {result.additions_applied
                .map((a) => `${a.sheet_name} row ${a.row} — ${a.label} (${a.cells_written} cells)`)
                .join("; ")}
            </p>
          ) : null}
          {result.rule_violations?.length ? (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              <p className="font-medium">
                {result.rule_violations.length} fill{result.rule_violations.length === 1 ? "" : "s"} contradict the
                template&apos;s sign conventions (written, flagged for review):
              </p>
              <ul className="mt-1 space-y-0.5 text-xs">
                {result.rule_violations.slice(0, 8).map((v, i) => (
                  <li key={i}>
                    <span className="font-mono">{v.template_sheet}!{v.template_cell}</span> {v.metric} ={" "}
                    {String(v.value)} but the template expects {v.expected}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge tone="green" className="px-2.5 py-1 text-xs">{result.summary.filled} filled</Badge>
            <Badge tone="warn" className="px-2.5 py-1 text-xs">{result.cleared_count} cleared</Badge>
            {result.cleared_values != null ? (
              <Badge className="px-2.5 py-1 text-xs">{result.cleared_values} values cleared</Badge>
            ) : null}
            {result.cleared_formulas != null ? (
              <Badge className="px-2.5 py-1 text-xs">{result.cleared_formulas} formulas cleared</Badge>
            ) : null}
            <Badge className="px-2.5 py-1 text-xs">{result.unmatched_count} unmatched</Badge>
            <Badge className="px-2.5 py-1 text-xs">{result.skipped_count} skipped</Badge>
            <Badge className="px-2.5 py-1 text-xs">{result.links_count} links</Badge>
            {result.filled_url ? (
              <a href={result.filled_url} className="ml-auto rounded-md bg-ink px-3 py-1.5 text-xs font-medium text-neutral-50 transition hover:bg-neutral-700">
                Download filled workbook
              </a>
            ) : null}
            {result.audit_url ? (
              <a href={result.audit_url} className="rounded-md border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-700 transition hover:bg-neutral-50">
                Audit (JSON)
              </a>
            ) : null}
          </div>
          {result.filled.length > 0 ? (
            <div className="tempo-scroll max-h-80 overflow-auto rounded-md border border-neutral-200">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-neutral-50 text-neutral-500">
                  <tr>
                    <th className="px-2 py-1.5 font-medium">template cell</th>
                    <th className="px-2 py-1.5 font-medium">← source</th>
                    <th className="px-2 py-1.5 font-medium">value</th>
                    <th className="px-2 py-1.5 font-medium">metric</th>
                    <th className="px-2 py-1.5 font-medium">period · scenario</th>
                  </tr>
                </thead>
                <tbody>
                  {result.filled.slice(0, 100).map((f, i) => (
                    <tr key={i} className="border-t border-neutral-100">
                      <td className="px-2 py-1 font-mono">{f.template_sheet}!{f.template_cell}</td>
                      <td className="px-2 py-1 font-mono text-neutral-400">{f.source_sheet}!{f.source_cell}</td>
                      <td className="px-2 py-1">{String(f.value)}</td>
                      <td className="px-2 py-1">{f.metric}</td>
                      <td className="px-2 py-1 text-neutral-500">{f.period_index ?? "—"} · {f.scenario ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-neutral-500">
              Nothing matched — check the source has the metrics this template needs.
            </p>
          )}
          {result.proposed_additions?.length ? (
            <div className="rounded-md border border-neutral-200">
              <p className="border-b border-neutral-200 bg-neutral-50 px-3 py-2 text-xs font-medium text-neutral-600">
                Proposed new line items (not written)
              </p>
              <table className="w-full text-left text-xs">
                <thead className="text-neutral-500">
                  <tr>
                    <th className="px-2 py-1 font-medium">Sheet</th>
                    <th className="px-2 py-1 font-medium">Row</th>
                    <th className="px-2 py-1 font-medium">Label</th>
                    <th className="px-2 py-1 font-medium">Unit</th>
                    <th className="px-2 py-1 font-medium">#values</th>
                  </tr>
                </thead>
                <tbody>
                  {result.proposed_additions.map((a, i) => (
                    <tr key={i} className="border-t border-neutral-100">
                      <td className="px-2 py-1">{a.sheet_name}</td>
                      <td className="px-2 py-1 font-mono">{a.row}</td>
                      <td className="px-2 py-1">{a.label}</td>
                      <td className="px-2 py-1 text-neutral-500">{a.unit ?? "—"}</td>
                      <td className="px-2 py-1">{a.values.length}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="border-t border-neutral-200 px-3 py-2 text-xs text-neutral-500">
                Re-run with New line items = Apply to write them.
              </p>
            </div>
          ) : null}
          {result.unmatched_reasons && result.unmatched_reasons.length > 0 ? (
            <div className="rounded-md border border-neutral-200 bg-neutral-50 px-3 py-2">
              <p className="mb-1 text-xs font-medium text-neutral-600">Why cells were left blank</p>
              <ul className="space-y-0.5 text-xs text-neutral-600">
                {result.unmatched_reasons.map((r, i) => (
                  <li key={i}>
                    <span className="font-mono text-neutral-400">{r.count}×</span> {r.reason}
                  </li>
                ))}
              </ul>
              {result.unmapped_metrics?.length ? (
                <p className="mt-2 text-xs text-neutral-500">
                  <span className="font-medium text-neutral-600">
                    No source data for {result.unmapped_metrics.length} template metric
                    {result.unmapped_metrics.length === 1 ? "" : "s"}:{" "}
                  </span>
                  {result.unmapped_metrics.slice(0, 20).join(" · ")}
                  {result.unmapped_metrics.length > 20 ? " …" : ""}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </Panel>
  );
}

/* --- Open questions --------------------------------------------------------- */

// Inline one-tap clarification panel — machine-checkable items are
// auto-verified on load so only human-judgment questions remain.
// Best-effort: hidden on any load failure.
function OpenQuestionsPanel({
  templateId,
  refreshKey,
}: {
  templateId: string;
  refreshKey: number;
}) {
  const [items, setItems] = useState<import("@/lib/templates").ReviewItem[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [autoVerified, setAutoVerified] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await getReviewItems(templateId);
        let open = list.items.filter((i) => i.status === "open");
        // auto-verify machine-checkables once (capped) — no human needed for those
        if (!autoVerified) {
          const machine = open.filter((i) => i.kind === "machine_checkable").slice(0, 10);
          for (const m of machine) {
            try {
              await verifyReviewItem(templateId, m.id);
            } catch {
              /* verification is best-effort */
            }
          }
          if (machine.length) {
            const relisted = await getReviewItems(templateId);
            open = relisted.items.filter((i) => i.status === "open");
          }
          if (!cancelled) setAutoVerified(true);
        }
        if (!cancelled) setItems(open);
      } catch {
        if (!cancelled) setItems(null); // hidden silently
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templateId, refreshKey]);

  async function answer(id: string, answer: string) {
    setBusy(id);
    try {
      await answerReviewItem(templateId, id, { status: "answered", answer });
      setItems((prev) => (prev ?? []).filter((i) => i.id !== id));
    } catch {
      /* leave the item; the contract page has full error handling */
    } finally {
      setBusy(null);
    }
  }

  if (!items || items.length === 0) return null;
  const shown = items.slice(0, 5);
  return (
    <Panel className="border-role-calc/25 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base">
          {items.length} question{items.length === 1 ? "" : "s"} for you
        </h2>
        <p className="text-xs text-neutral-400">Answers persist and improve every future run.</p>
        <Link
          href={`/template/${templateId}/contract`}
          className="ml-auto text-xs font-medium text-neutral-500 underline decoration-neutral-300 underline-offset-2 transition hover:text-ink"
        >
          See all →
        </Link>
      </div>
      <ul className="mt-3 grid gap-2 lg:grid-cols-2">
        {shown.map((q) => (
          <li key={q.id} className="rounded-lg border border-neutral-200 bg-neutral-50/60 p-3">
            <p className="text-sm leading-snug">{q.question}</p>
            {q.suggested_answer ? (
              <p className="mt-1 text-xs text-neutral-500">Suggested: {q.suggested_answer}</p>
            ) : null}
            <div className="mt-2 flex flex-wrap gap-2">
              {q.suggested_answer ? (
                <Button
                  variant="positive"
                  size="xs"
                  onClick={() => void answer(q.id, q.suggested_answer!)}
                  disabled={busy === q.id}
                >
                  {busy === q.id ? "Saving…" : "Yes — confirm"}
                </Button>
              ) : null}
              <LinkButton href={`/template/${templateId}/contract`} variant="secondary" size="xs">
                {q.suggested_answer ? "No / different…" : "Answer…"}
              </LinkButton>
            </div>
          </li>
        ))}
      </ul>
      {items.length > shown.length ? (
        <p className="mt-2 text-xs text-neutral-400">+{items.length - shown.length} more in the inbox.</p>
      ) : null}
    </Panel>
  );
}

// Small header chip linking to the contract page's Questions inbox.
function OpenQuestionsChip({ templateId }: { templateId: string }) {
  const [openCount, setOpenCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    getReviewItems(templateId)
      .then((r) => {
        if (!cancelled) setOpenCount(r.open_count);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [templateId]);

  if (!openCount) return null;
  return (
    <Link href={`/template/${templateId}/contract`}>
      <Badge tone="warn" className="px-2.5 py-1 text-xs transition hover:bg-amber-100">
        {openCount} open question{openCount === 1 ? "" : "s"}
      </Badge>
    </Link>
  );
}

/* --- Extensible regions ----------------------------------------------------- */

function formatRules(rules: unknown): string | null {
  if (rules == null) return null;
  if (typeof rules === "string") return rules;
  if (Array.isArray(rules)) return rules.map(String).join(" · ");
  if (typeof rules === "object") {
    return Object.entries(rules as Record<string, unknown>)
      .map(([k, v]) => `${k}: ${String(v)}`)
      .join(" · ");
  }
  return String(rules);
}

function RegionsPanel({ templateId }: { templateId: string }) {
  const [regions, setRegions] = useState<ExtensibleRegion[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRegions(await getRegions(templateId));
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load regions");
    }
  }, [templateId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleDetect() {
    setDetecting(true);
    setError(null);
    try {
      setRegions(await detectRegions(templateId));
      setLoadError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Region detection failed");
    } finally {
      setDetecting(false);
    }
  }

  return (
    <Panel className="p-5">
      <SectionHeader
        title="Extensible regions"
        hint="Where new line items may be inserted during population."
      >
        <Button variant="secondary" onClick={handleDetect} disabled={detecting}>
          {detecting
            ? "Detecting… (may take a minute)"
            : regions?.length
              ? "Re-detect"
              : "Detect regions"}
        </Button>
      </SectionHeader>

      {error ? <div className="mt-3"><ErrorNote>{error}</ErrorNote></div> : null}

      {loadError ? (
        <p className="mt-3 text-sm text-neutral-500">
          Couldn&apos;t load regions ({loadError}).{" "}
          <button onClick={() => void load()} className="font-medium text-neutral-700 underline">
            Retry
          </button>
        </p>
      ) : regions === null ? (
        <p className="mt-3 text-sm text-neutral-400">Loading…</p>
      ) : regions.length === 0 ? (
        <p className="mt-3 text-sm text-neutral-400">None detected yet.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {regions.map((r, i) => {
            const slots = r.slots ?? [];
            const nBlank = slots.filter((s) => s.mode === "blank").length;
            const nPlaceholder = slots.filter((s) => s.mode === "placeholder").length;
            const nEditable = slots.filter((s) => s.mode === "editable_label").length;
            const placeholderLabels = slots
              .filter((s) => s.mode !== "blank" && s.current_label)
              .map((s) => s.current_label)
              .slice(0, 6);
            return (
              <li key={r.id ?? i} className="rounded-lg border border-neutral-200 p-3">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-medium">{r.sheet_name}</span>
                  {r.kind ? <Badge>{r.kind}</Badge> : null}
                  <span className="font-mono text-xs text-neutral-400">
                    rows {r.row_start}–{r.row_end}
                  </span>
                  {slots.length > 0 ? (
                    <span className="text-xs text-neutral-500">
                      {[
                        nBlank ? `${nBlank} blank` : null,
                        nPlaceholder ? `${nPlaceholder} placeholder` : null,
                        nEditable ? `${nEditable} editable` : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  ) : r.capacity != null ? (
                    <span className="text-xs text-neutral-500">capacity {r.capacity}</span>
                  ) : null}
                </div>
                {placeholderLabels.length ? (
                  <p className="mt-1 text-xs text-neutral-400">
                    Replaceable labels: {placeholderLabels.join(" · ")}
                  </p>
                ) : null}
                {formatRules(r.rules) ? (
                  <p className="mt-1.5 text-xs text-neutral-500">{formatRules(r.rules)}</p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

/* --- Page ------------------------------------------------------------------- */

export default function TemplatePage() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<Understanding | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Load failure (e.g. parser unreachable) is distinct from "not analysed":
  // it must NOT show the run-understanding CTA, which triggers an expensive re-run.
  const [loadError, setLoadError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [ready, setReady] = useState(false);
  const [questionsRefresh, setQuestionsRefresh] = useState(0);
  // Live-onboarding state: sheet names from the fast structural parse, and
  // whether the anatomy should play its build-in animation (run just landed).
  const [parsedSheets, setParsedSheets] = useState<string[] | null>(null);
  const [startedAt, setStartedAt] = useState(0);
  const [buildAnimation, setBuildAnimation] = useState(false);
  const finishedRef = useRef(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await getUnderstanding(id));
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setReady(true);
    }
  }, [id]);

  useEffect(() => {
    void load();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [load]);

  function handleRetryLoad() {
    setReady(false);
    setLoadError(null);
    void load();
  }

  async function handleRun() {
    setRunning(true);
    setError(null);
    setBuildAnimation(false);
    setStartedAt(Date.now());
    finishedRef.current = false;

    const firstRun = !data?.available;

    // Fast structural parse → real sheet names for the reading state.
    parseTemplate(id)
      .then((s) =>
        setParsedSheets(s.sheets.filter((x) => !x.is_hidden).map((x) => x.name))
      )
      .catch(() => setParsedSheets(null));

    // The understanding persists at the END of the long call. On a first run we
    // also poll — if the held connection drops, the result still lands.
    const finish = async () => {
      if (finishedRef.current) return;
      finishedRef.current = true;
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
      await load();
      setBuildAnimation(true); // animate the real structure building in
      setRunning(false);
      setQuestionsRefresh((k) => k + 1);
    };

    if (firstRun) {
      pollRef.current = setInterval(async () => {
        try {
          const probe = await getUnderstanding(id);
          if (probe.available) void finish();
        } catch {
          /* keep polling */
        }
      }, 10_000);
    }

    try {
      await understandTemplate(id);
      await finish();
    } catch (e) {
      if (!finishedRef.current) {
        if (pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
        setError(e instanceof Error ? e.message : "Understanding failed");
        setRunning(false);
      }
    }
  }

  const wb = data?.workbook;

  return (
    <div>
      <BackLink href="/">Library</BackLink>

      {error ? <div className="mt-4"><ErrorNote>{error}</ErrorNote></div> : null}

      {!ready ? (
        <div className="mt-8 space-y-4">
          <div className="shimmer h-8 w-72 rounded" />
          <div className="shimmer h-40 w-full rounded-xl" />
        </div>
      ) : running ? (
        <div className="mt-6">
          <header className="mb-6 flex items-center gap-3">
            <h1 className="text-[1.9rem] leading-tight">Onboarding this template</h1>
          </header>
          <OnboardingLive sheetNames={parsedSheets} startedAt={startedAt} />
        </div>
      ) : loadError ? (
        <div className="mt-8">
          <EmptyState
            title="Couldn't reach the analysis service"
            body="The template's understanding couldn't be loaded — the analysis service may be down. This doesn't mean the template hasn't been analysed."
          >
            <p className="mb-4 text-xs text-neutral-400">{loadError}</p>
            <Button variant="primary" size="md" onClick={handleRetryLoad}>
              Retry
            </Button>
          </EmptyState>
        </div>
      ) : !data?.available ? (
        <div className="mt-16 flex flex-col items-center text-center">
          <p className="text-[11px] font-medium uppercase tracking-[0.18em] text-neutral-400">
            New template
          </p>
          <h1 className="mt-2 max-w-xl text-[2.1rem] leading-tight">
            Let Tempo read this workbook
          </h1>
          <p className="mt-3 max-w-md text-sm text-neutral-500">
            Every sheet studied, inputs and calculations mapped, structure built. A few minutes.
          </p>
          <Button
            variant="primary"
            size="md"
            className="mt-7 px-6"
            onClick={handleRun}
            disabled={running}
          >
            Begin onboarding
          </Button>
        </div>
      ) : (
        <div className="mt-4 space-y-6">
          <header className="flex flex-wrap items-end gap-3">
            <div className="min-w-0">
              <p className="mb-1 text-[11px] font-medium uppercase tracking-[0.16em] text-neutral-400">
                Template
              </p>
              <h1 className="text-[1.9rem] leading-tight">
                {wb?.archetype ?? "Workbook"}
              </h1>
            </div>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <OpenQuestionsChip templateId={id} />
              <LinkButton href={`/template/${id}/timeseries`} variant="secondary">
                Time series
              </LinkButton>
              <LinkButton href={`/template/${id}/contract`} variant="secondary">
                Contract
              </LinkButton>
              <Button variant="ghost" onClick={handleRun} disabled={running}>
                Re-run
              </Button>
            </div>
          </header>

          <OpenQuestionsPanel templateId={id} refreshKey={questionsRefresh} />

          <AnatomyView data={data} animate={buildAnimation} />

          <PopulatePanel templateId={id} />

          <RegionsPanel templateId={id} />
        </div>
      )}
    </div>
  );
}
