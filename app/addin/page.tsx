"use client";

// Tempo task pane — runs inside Excel (sideloaded manifest points here).
// Flow: pick an onboarded template → the OPEN workbook is serialized in place
// via Office.js → parser populates → the filled workbook opens as a new file.
// Designed for a ~320px pane: row list, truncation everywhere, staged progress.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  bufferToBase64,
  openWorkbookFromBase64,
  serializeWorkbook,
} from "./excel";

/* global Office */

type TemplateCard = {
  id: string;
  name: string;
  archetype: string | null;
  understood: boolean;
  thumbnailUrl: string | null;
};

type PopulateResult = {
  links_count: number;
  reconciled_count: number;
  open_questions_count: number;
  review_count: number;
  filled_url: string | null;
  error?: string;
};

type Phase =
  | { kind: "boot" }
  | { kind: "no-office" }
  | { kind: "ready" }
  | { kind: "reading"; template: TemplateCard; startedAt: number }
  | { kind: "filling"; template: TemplateCard; startedAt: number }
  | { kind: "done"; template: TemplateCard; result: PopulateResult }
  | { kind: "error"; message: string };

type OpenState = "idle" | "opening" | "opened" | "failed";

const OFFICE_JS = "https://appsforoffice.microsoft.com/lib/1/hosted/office.js";

// REAL stages: the parser stamps its current stage (app/population/progress.py)
// and the pane polls /api/v1/populate-progress while a fill is running.
const STAGE_ORDER = [
  "reading",
  "understanding",
  "planning",
  "verifying",
  "writing",
] as const;
const STAGE_LABELS: Record<(typeof STAGE_ORDER)[number], string> = {
  reading: "Reading your workbook",
  understanding: "Understanding your data",
  planning: "Mapping series onto the template",
  verifying: "Verifying the plan",
  writing: "Writing the filled file",
};

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

function Spinner() {
  return (
    <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-[2px] border-neutral-300 border-t-ink" />
  );
}

function Check() {
  return (
    <svg viewBox="0 0 16 16" className="h-3.5 w-3.5 text-ink" fill="none">
      <path d="M3 8.5l3.2 3L13 4.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function AddinPage() {
  const [phase, setPhase] = useState<Phase>({ kind: "boot" });
  const [templates, setTemplates] = useState<TemplateCard[] | null>(null);
  const [openState, setOpenState] = useState<OpenState>("idle");
  const officeHost = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const boot = async () => {
      if (!document.querySelector(`script[src="${OFFICE_JS}"]`)) {
        await new Promise<void>((resolve) => {
          const s = document.createElement("script");
          s.src = OFFICE_JS;
          s.onload = () => resolve();
          s.onerror = () => resolve();
          document.head.appendChild(s);
        });
      }
      const w = window as unknown as { Office?: typeof Office };
      if (w.Office?.onReady) {
        const info = await w.Office.onReady();
        officeHost.current = info.host === Office.HostType.Excel;
      }
      if (!cancelled) {
        setPhase(officeHost.current ? { kind: "ready" } : { kind: "no-office" });
      }
    };
    boot();
    fetch("/api/v1/templates")
      .then((r) => r.json())
      .then((rows: TemplateCard[]) => {
        if (!cancelled) {
          setTemplates(Array.isArray(rows) ? rows.filter((t) => t.understood) : []);
        }
      })
      .catch(() => {
        if (!cancelled) setTemplates([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const run = useCallback(async (template: TemplateCard) => {
    const startedAt = Date.now();
    setOpenState("idle");
    autoOpened.current = null;
    try {
      setPhase({ kind: "reading", template, startedAt });
      const snapshot = await serializeWorkbook();
      if (!snapshot.sheets.some((s) => s.cells.length > 0)) {
        setPhase({ kind: "error", message: "The open workbook has no data to read." });
        return;
      }
      setPhase({ kind: "filling", template, startedAt });
      const res = await fetch(`/api/v1/populate-workbook/${template.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: snapshot.metadata.filename, snapshot }),
      });
      const result = (await res.json()) as PopulateResult & { detail?: string };
      if (!res.ok) {
        setPhase({
          kind: "error",
          message: result.detail ?? result.error ?? `Populate failed (${res.status})`,
        });
        return;
      }
      setPhase({ kind: "done", template, result });
    } catch (e) {
      setPhase({
        kind: "error",
        message: e instanceof Error ? e.message : "Something went wrong.",
      });
    }
  }, []);

  // "Take me to the Excel": three tiers, most-native first.
  //   1. Excel.createWorkbook — new workbook opens & focuses inside Excel.
  //   2. /api/v1/open-local — the dev server downloads the file and launches
  //      Excel on it natively (createWorkbook rejects very large payloads).
  //   3. window.open on the signed URL — plain browser download.
  const openFilled = useCallback(
    async (result: PopulateResult, template?: TemplateCard) => {
      if (!result.filled_url) return;
      setOpenState("opening");
      try {
        const res = await fetch(
          `/api/v1/filled-file?url=${encodeURIComponent(result.filled_url)}`
        );
        if (!res.ok) throw new Error(`Download failed (${res.status})`);
        const buf = await res.arrayBuffer();
        await openWorkbookFromBase64(bufferToBase64(buf));
        setOpenState("opened");
        return;
      } catch {
        // tier 2: native local open
      }
      try {
        const res = await fetch("/api/v1/open-local", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            url: result.filled_url,
            name: `${template?.name ?? "template"} — filled.xlsx`,
          }),
        });
        if (!res.ok) throw new Error(`Local open failed (${res.status})`);
        setOpenState("opened");
        return;
      } catch {
        // tier 3: browser download
      }
      try {
        window.open(result.filled_url, "_blank");
        setOpenState("opened");
      } catch {
        setOpenState("failed");
      }
    },
    []
  );

  // Auto-open the filled workbook the moment the run completes.
  const autoOpened = useRef<string | null>(null);
  useEffect(() => {
    if (phase.kind !== "done" || !phase.result.filled_url) return;
    if (autoOpened.current === phase.result.filled_url) return;
    autoOpened.current = phase.result.filled_url;
    openFilled(phase.result, phase.template);
  }, [phase, openFilled]);

  const busy = phase.kind === "reading" || phase.kind === "filling";
  const now = useNow(busy);

  // Poll the engine's real stage while a fill is in flight.
  const [serverStage, setServerStage] = useState<{
    stage: string | null;
    detail: string | null;
  }>({ stage: null, detail: null });
  const fillingId = phase.kind === "filling" ? phase.template.id : null;
  useEffect(() => {
    if (!fillingId) {
      setServerStage({ stage: null, detail: null });
      return;
    }
    let stop = false;
    const poll = async () => {
      try {
        const r = await fetch(`/api/v1/populate-progress/${fillingId}`, {
          cache: "no-store",
        });
        const j = (await r.json()) as { stage?: string; detail?: string };
        if (!stop) {
          setServerStage({ stage: j.stage ?? null, detail: j.detail ?? null });
        }
      } catch {
        /* keep last known stage */
      }
    };
    poll();
    const t = setInterval(poll, 2500);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [fillingId]);

  // ---- busy: full-pane staged progress ----
  if (busy) {
    const startedAt = phase.startedAt;
    const secs = Math.max(0, Math.floor((now - startedAt) / 1000));
    const mm = Math.floor(secs / 60);
    const ss = String(secs % 60).padStart(2, "0");
    const activeIdx =
      phase.kind === "reading"
        ? 0
        : Math.max(
            1,
            STAGE_ORDER.indexOf(
              (serverStage.stage ?? "understanding") as (typeof STAGE_ORDER)[number]
            )
          );
    return (
      <main className="px-4 py-5">
        <p className="truncate text-[11px] font-medium uppercase tracking-[0.14em] text-neutral-400">
          Filling
        </p>
        <h1
          className="mt-0.5 truncate text-[17px] font-semibold text-ink"
          style={{ fontFamily: "var(--font-display)" }}
        >
          {phase.template.name}
        </h1>

        <div className="mt-4 h-1 overflow-hidden rounded-full bg-neutral-200">
          <div className="h-full w-1/3 animate-[pane-slide_1.6s_ease-in-out_infinite] rounded-full bg-ink" />
        </div>

        <ol className="mt-5 space-y-3.5">
          {STAGE_ORDER.map((key, i) => {
            const state = i < activeIdx ? "done" : i === activeIdx ? "active" : "todo";
            return (
              <li key={key} className="flex items-center gap-2.5">
                <span className="flex h-5 w-5 shrink-0 items-center justify-center">
                  {state === "done" ? (
                    <Check />
                  ) : state === "active" ? (
                    <Spinner />
                  ) : (
                    <span className="h-1.5 w-1.5 rounded-full bg-neutral-300" />
                  )}
                </span>
                <span
                  className={
                    state === "todo"
                      ? "text-[13px] text-neutral-400"
                      : state === "active"
                        ? "text-[13px] font-medium text-ink"
                        : "text-[13px] text-neutral-500"
                  }
                >
                  {STAGE_LABELS[key]}
                  {state === "active" && serverStage.detail ? (
                    <span className="ml-1.5 text-[11px] font-normal text-neutral-400">
                      {serverStage.detail}
                    </span>
                  ) : null}
                </span>
              </li>
            );
          })}
        </ol>

        <div className="mt-6 flex items-center justify-between border-t border-neutral-200/80 pt-3">
          <span className="text-[12px] text-neutral-400">
            First run on new data takes a few minutes
          </span>
          <span className="font-mono text-[12px] tabular-nums text-neutral-500">
            {mm}:{ss}
          </span>
        </div>
        <style>{`@keyframes pane-slide { 0% { margin-left: -35%; } 100% { margin-left: 100%; } }`}</style>
      </main>
    );
  }

  // ---- done: result card ----
  if (phase.kind === "done") {
    const r = phase.result;
    return (
      <main className="px-4 py-5">
        <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-neutral-400">
          Complete
        </p>
        <h1
          className="mt-0.5 truncate text-[17px] font-semibold text-ink"
          style={{ fontFamily: "var(--font-display)" }}
        >
          {phase.template.name}
        </h1>

        <div className="mt-4 grid grid-cols-2 gap-2">
          <div className="rounded-lg border border-neutral-200 bg-white px-3 py-2.5">
            <div className="text-[20px] font-semibold leading-tight text-ink">
              {r.links_count}
            </div>
            <div className="mt-0.5 text-[11px] text-neutral-500">cells filled</div>
          </div>
          <div className="rounded-lg border border-neutral-200 bg-white px-3 py-2.5">
            <div className="text-[20px] font-semibold leading-tight text-ink">
              {r.open_questions_count + r.review_count}
            </div>
            <div className="mt-0.5 text-[11px] text-neutral-500">to review</div>
          </div>
        </div>

        <button
          onClick={() => openFilled(r, phase.template)}
          disabled={!r.filled_url || openState === "opening"}
          className="mt-4 w-full rounded-lg bg-ink px-3 py-2.5 text-[13px] font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50"
        >
          {openState === "opening"
            ? "Opening in Excel…"
            : openState === "opened"
              ? "Open again"
              : "Open filled workbook"}
        </button>
        {openState === "opened" && (
          <p className="mt-2 text-center text-[11px] text-neutral-400">
            The filled workbook opened in a new Excel window.
          </p>
        )}
        {openState === "failed" && (
          <p className="mt-2 break-words text-center text-[11px] text-red-700">
            Could not open automatically —{" "}
            <a href={r.filled_url ?? "#"} target="_blank" className="underline">
              download it here
            </a>
            .
          </p>
        )}

        <div className="mt-3 flex items-center justify-between text-[12px]">
          <a
            href={`/template/${phase.template.id}`}
            target="_blank"
            className="text-neutral-500 underline-offset-2 hover:text-ink hover:underline"
          >
            Review in Tempo
          </a>
          <button
            onClick={() => setPhase({ kind: "ready" })}
            className="text-neutral-500 underline-offset-2 hover:text-ink hover:underline"
          >
            Fill another
          </button>
        </div>
      </main>
    );
  }

  // ---- picker (default) ----
  return (
    <main className="px-4 py-4">
      <h1 className="text-[13px] font-medium text-ink">Fill a template</h1>
      <p className="mt-0.5 text-[12px] leading-snug text-neutral-500">
        Reads the open workbook in place — nothing is uploaded.
      </p>

      {phase.kind === "no-office" && (
        <div className="mt-3 rounded-lg border border-amber-200/80 bg-amber-50 px-3 py-2.5 text-[12px] leading-snug text-amber-900">
          Open this pane from inside Excel (Home → Open Tempo) to read the
          active workbook.
        </div>
      )}

      {phase.kind === "error" && (
        <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2.5">
          <p className="break-words text-[12px] leading-snug text-red-900">
            {phase.message}
          </p>
          <button
            onClick={() => setPhase({ kind: "ready" })}
            className="mt-2 rounded-md bg-ink px-2.5 py-1 text-[11px] font-medium text-white"
          >
            Try again
          </button>
        </div>
      )}

      <ul className="mt-3 divide-y divide-neutral-100 overflow-hidden rounded-lg border border-neutral-200 bg-white">
        {templates === null &&
          [0, 1, 2].map((i) => (
            <li key={i} className="flex animate-pulse items-center gap-3 px-3 py-2.5">
              <span className="h-9 w-9 shrink-0 rounded-md bg-neutral-100" />
              <span className="h-3 w-2/3 rounded bg-neutral-100" />
            </li>
          ))}
        {(templates ?? []).map((t) => (
          <li key={t.id}>
            <button
              onClick={() => run(t)}
              disabled={phase.kind === "boot"}
              className="group flex w-full items-center gap-3 px-3 py-2.5 text-left transition hover:bg-neutral-50 disabled:opacity-60"
            >
              {t.thumbnailUrl ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={t.thumbnailUrl}
                  alt=""
                  className="h-9 w-9 shrink-0 rounded-md border border-neutral-200 object-cover object-left-top"
                />
              ) : (
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-neutral-200 bg-neutral-50 text-[11px] font-semibold text-neutral-400">
                  {t.name.slice(0, 1).toUpperCase()}
                </span>
              )}
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] font-medium text-ink">
                  {t.name}
                </span>
                <span className="block truncate text-[11px] text-neutral-400">
                  {t.archetype ?? "Template"}
                </span>
              </span>
              <svg
                viewBox="0 0 16 16"
                className="h-3.5 w-3.5 shrink-0 text-neutral-300 transition group-hover:text-ink"
                fill="none"
              >
                <path d="M6 3.5L10.5 8L6 12.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </li>
        ))}
        {templates !== null && templates.length === 0 && (
          <li className="px-3 py-4 text-center text-[12px] text-neutral-400">
            No onboarded templates yet — add one in Tempo first.
          </li>
        )}
      </ul>
    </main>
  );
}
