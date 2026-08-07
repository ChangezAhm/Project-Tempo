"use client";

// Tempo task pane — runs inside Excel (sideloaded manifest points here).
// Flow: pick an onboarded template → the OPEN workbook is serialized in place
// via Office.js → parser populates → the filled workbook opens as a new file.

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
  additions_applied?: unknown[];
  filled_url: string | null;
  error?: string;
};

type Phase =
  | { kind: "boot" }
  | { kind: "no-office" }
  | { kind: "ready" }
  | { kind: "reading"; template: TemplateCard }
  | { kind: "filling"; template: TemplateCard; startedAt: number }
  | { kind: "done"; template: TemplateCard; result: PopulateResult }
  | { kind: "error"; message: string };

const OFFICE_JS = "https://appsforoffice.microsoft.com/lib/1/hosted/office.js";

function useElapsed(active: boolean, since: number | null) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  if (!active || since === null) return "";
  const s = Math.max(0, Math.floor((now - since) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export default function AddinPage() {
  const [phase, setPhase] = useState<Phase>({ kind: "boot" });
  const [templates, setTemplates] = useState<TemplateCard[]>([]);
  const [opening, setOpening] = useState(false);
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
        if (!cancelled && Array.isArray(rows)) {
          setTemplates(rows.filter((t) => t.understood));
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const run = useCallback(async (template: TemplateCard) => {
    try {
      setPhase({ kind: "reading", template });
      const snapshot = await serializeWorkbook();
      const nonEmpty = snapshot.sheets.filter((s) => s.cells.length > 0);
      if (nonEmpty.length === 0) {
        setPhase({ kind: "error", message: "The open workbook has no data to read." });
        return;
      }
      setPhase({ kind: "filling", template, startedAt: Date.now() });
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

  const openFilled = useCallback(async (result: PopulateResult) => {
    if (!result.filled_url) return;
    setOpening(true);
    try {
      const res = await fetch(
        `/api/v1/filled-file?url=${encodeURIComponent(result.filled_url)}`
      );
      if (!res.ok) throw new Error(`Download failed (${res.status})`);
      const buf = await res.arrayBuffer();
      await openWorkbookFromBase64(bufferToBase64(buf));
    } catch (e) {
      setPhase({
        kind: "error",
        message: e instanceof Error ? e.message : "Could not open the filled workbook.",
      });
    } finally {
      setOpening(false);
    }
  }, []);

  const busy = phase.kind === "reading" || phase.kind === "filling";
  const elapsed = useElapsed(
    phase.kind === "filling",
    phase.kind === "filling" ? phase.startedAt : null
  );

  return (
    <main className="mx-auto w-full max-w-md px-4 py-5">
      <h1
        className="text-xl font-semibold tracking-tight text-ink"
        style={{ fontFamily: "var(--font-display)" }}
      >
        Fill a template
      </h1>
      <p className="mt-1 text-[13px] leading-relaxed text-neutral-500">
        Your open workbook is read in place — nothing is uploaded as a file.
        Pick where the data should go.
      </p>

      {phase.kind === "no-office" && (
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-3.5 py-3 text-[13px] text-amber-900">
          This page is the Tempo Excel task pane. Open it from inside Excel
          (Home&nbsp;→ Tempo) to read the active workbook.
        </div>
      )}

      {phase.kind === "error" && (
        <div className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3.5 py-3 text-[13px] text-red-900">
          {phase.message}
          <button
            onClick={() => setPhase({ kind: "ready" })}
            className="mt-2 block rounded-md bg-ink px-3 py-1.5 text-[12px] font-medium text-white"
          >
            Try again
          </button>
        </div>
      )}

      {phase.kind === "done" && (
        <div className="mt-4 rounded-lg border border-neutral-200 bg-white p-4 shadow-sm">
          <div className="text-[13px] font-medium text-ink">
            {phase.template.name}
          </div>
          <div className="mt-2 grid grid-cols-2 gap-2 text-[12px] text-neutral-600">
            <div className="rounded-md bg-neutral-50 px-2.5 py-2">
              <span className="block text-lg font-semibold text-ink">
                {phase.result.links_count}
              </span>
              cells filled
            </div>
            <div className="rounded-md bg-neutral-50 px-2.5 py-2">
              <span className="block text-lg font-semibold text-ink">
                {phase.result.open_questions_count + phase.result.review_count}
              </span>
              to review
            </div>
          </div>
          <button
            onClick={() => openFilled(phase.result)}
            disabled={!phase.result.filled_url || opening}
            className="mt-3 w-full rounded-md bg-ink px-3 py-2 text-[13px] font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50"
          >
            {opening ? "Opening…" : "Open filled workbook"}
          </button>
          <div className="mt-2 flex items-center justify-between text-[12px]">
            <a
              href={`/template/${phase.template.id}`}
              target="_blank"
              className="text-neutral-500 underline-offset-2 hover:underline"
            >
              Review questions in Tempo
            </a>
            <button
              onClick={() => setPhase({ kind: "ready" })}
              className="text-neutral-500 underline-offset-2 hover:underline"
            >
              Fill another
            </button>
          </div>
        </div>
      )}

      {busy && (
        <div className="mt-4 rounded-lg border border-neutral-200 bg-white p-4 shadow-sm">
          <div className="flex items-center gap-2.5">
            <span className="h-2 w-2 animate-pulse rounded-full bg-ink" />
            <span className="text-[13px] text-ink">
              {phase.kind === "reading"
                ? "Reading the open workbook…"
                : `Filling ${"template" in phase ? phase.template.name : ""}…`}
            </span>
            {elapsed && (
              <span className="ml-auto font-mono text-[12px] text-neutral-400">
                {elapsed}
              </span>
            )}
          </div>
          {phase.kind === "filling" && (
            <p className="mt-2 text-[12px] leading-relaxed text-neutral-500">
              Mapping your series onto the template, verifying, and writing the
              filled file. This can take a few minutes on first run.
            </p>
          )}
        </div>
      )}

      {(phase.kind === "ready" || phase.kind === "boot") && (
        <ul className="mt-4 space-y-2.5">
          {templates.map((t) => (
            <li key={t.id}>
              <button
                onClick={() => run(t)}
                disabled={busy || phase.kind === "boot"}
                className="group w-full overflow-hidden rounded-lg border border-neutral-200 bg-white text-left shadow-sm transition hover:border-neutral-300 hover:shadow disabled:opacity-60"
              >
                {t.thumbnailUrl && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={t.thumbnailUrl}
                    alt=""
                    className="h-20 w-full border-b border-neutral-100 object-cover object-left-top"
                  />
                )}
                <div className="px-3.5 py-2.5">
                  <div className="text-[13px] font-medium text-ink">{t.name}</div>
                  <div className="mt-0.5 flex items-center justify-between text-[11px] text-neutral-400">
                    <span>{t.archetype ?? "Template"}</span>
                    <span className="font-medium text-ink opacity-0 transition group-hover:opacity-100">
                      Fill →
                    </span>
                  </div>
                </div>
              </button>
            </li>
          ))}
          {templates.length === 0 && phase.kind === "ready" && (
            <li className="rounded-lg border border-dashed border-neutral-300 px-3.5 py-4 text-center text-[12px] text-neutral-400">
              No onboarded templates yet — add one in Tempo first.
            </li>
          )}
        </ul>
      )}
    </main>
  );
}
