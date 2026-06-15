"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  getUnderstanding,
  populateTemplate,
  understandTemplate,
  type CriticalInput,
  type PopulateResult,
  type SheetUnderstanding,
  type Understanding,
} from "@/lib/templates";

const SOURCE_STYLE: Record<string, string> = {
  template_stated: "bg-emerald-50 text-emerald-700",
  model_knowledge: "bg-amber-50 text-amber-700",
  inferred: "bg-sky-50 text-sky-700",
};

const SOURCE_LABEL: Record<string, string> = {
  template_stated: "stated in template",
  model_knowledge: "domain knowledge",
  inferred: "inferred from formulas",
};

function InputCard({ ci }: { ci: CriticalInput }) {
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium leading-snug">{ci.label}</p>
        {ci.needs_value ? (
          <span className="shrink-0 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
            needs value
          </span>
        ) : null}
      </div>
      <p className="mt-1 font-mono text-[11px] text-neutral-400">
        {ci.cells.join(", ")}
        {ci.unit ? ` · ${ci.unit}` : ""}
      </p>
      {ci.definition ? (
        <p className="mt-2 text-xs text-neutral-600">{ci.definition}</p>
      ) : null}
      {ci.qualification_criteria ? (
        <p className="mt-1.5 text-xs text-neutral-500">
          <span className="font-medium text-neutral-600">Qualifies: </span>
          {ci.qualification_criteria}
        </p>
      ) : null}
      {ci.expected_source ? (
        <p className="mt-1.5 text-xs text-neutral-500">
          <span className="font-medium text-neutral-600">Source: </span>
          {ci.expected_source}
        </p>
      ) : null}
      {ci.interpretation_source ? (
        <span
          className={`mt-2 inline-block rounded px-1.5 py-0.5 text-[10px] font-medium ${
            SOURCE_STYLE[ci.interpretation_source] ?? "bg-neutral-100 text-neutral-600"
          }`}
        >
          {SOURCE_LABEL[ci.interpretation_source] ?? ci.interpretation_source}
        </span>
      ) : null}
    </div>
  );
}

function SheetPanel({
  sheet,
  inputs,
}: {
  sheet: SheetUnderstanding;
  inputs: CriticalInput[];
}) {
  return (
    <section className="rounded-xl border border-neutral-200 bg-white p-4">
      <div className="mb-3 flex items-center gap-2">
        <h3 className="font-medium">{sheet.sheet_name}</h3>
        {sheet.role ? (
          <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-[10px] font-medium text-neutral-500">
            {sheet.role}
          </span>
        ) : null}
      </div>
      {sheet.summary ? (
        <p className="mb-3 text-sm text-neutral-600">{sheet.summary}</p>
      ) : null}
      <div className="grid gap-4 lg:grid-cols-2">
        {sheet.snippet_url ? (
          <a href={sheet.snippet_url} target="_blank" rel="noreferrer" className="block">
            <img
              src={sheet.snippet_url}
              alt={`${sheet.sheet_name} input area`}
              className="w-full rounded-lg border border-neutral-200 bg-neutral-50"
            />
          </a>
        ) : null}
        {inputs.length > 0 ? (
          <div className="space-y-2.5">
            {inputs.map((ci) => (
              <InputCard key={ci.id} ci={ci} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-neutral-400">No distinct input areas captured for this sheet.</p>
        )}
      </div>
    </section>
  );
}

function PopulatePanel({ templateId }: { templateId: string }) {
  const [asOf, setAsOf] = useState("");
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
      setResult(await populateTemplate(templateId, file, asOf || null));
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
    <section className="rounded-xl border border-neutral-200 bg-white p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-medium">Populate this template</h2>
          <p className="mt-1 text-xs text-neutral-500">
            Drop a portfolio company’s data file and it fills this template’s inputs. The file isn’t saved as a template.
          </p>
        </div>
        <label className="text-xs text-neutral-600">
          As-of date (optional)
          <input
            type="date"
            value={asOf}
            onChange={(e) => setAsOf(e.target.value)}
            className="mt-1 block rounded-md border border-neutral-300 px-2 py-1.5 text-sm"
          />
        </label>
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
        className={`mt-3 flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center transition ${
          dragging
            ? "border-neutral-900 bg-neutral-50"
            : "border-neutral-300 hover:border-neutral-400 hover:bg-neutral-50/50"
        } ${running ? "pointer-events-none opacity-60" : ""}`}
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
            <p className="mt-1 text-xs text-neutral-500">Reading the file and matching it to this template — a few minutes.</p>
          </>
        ) : (
          <>
            <p className="text-sm font-medium text-neutral-700">Drop an Excel file here</p>
            <p className="mt-1 text-xs text-neutral-500">or click to choose · .xlsx, .xlsm, .xls</p>
          </>
        )}
      </div>

      {error ? <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-600">{error}</p> : null}

      {result ? (
        <div className="mt-4 space-y-3">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="rounded bg-emerald-50 px-2 py-1 text-emerald-700">{result.summary.filled} filled</span>
            <span className="rounded bg-amber-50 px-2 py-1 text-amber-700">{result.cleared_count} cleared</span>
            <span className="rounded bg-neutral-100 px-2 py-1 text-neutral-600">{result.unmatched_count} unmatched</span>
            <span className="rounded bg-neutral-100 px-2 py-1 text-neutral-600">{result.skipped_count} skipped</span>
            <span className="rounded bg-neutral-100 px-2 py-1 text-neutral-600">{result.links_count} links</span>
            {result.filled_url ? (
              <a href={result.filled_url} className="rounded-md bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-neutral-700">
                Download filled workbook
              </a>
            ) : null}
            {result.audit_url ? (
              <a href={result.audit_url} className="rounded-md border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-700 hover:bg-neutral-50">
                Download audit (JSON)
              </a>
            ) : null}
          </div>
          {result.filled.length > 0 ? (
            <div className="max-h-80 overflow-auto rounded-md border border-neutral-200">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-neutral-50 text-neutral-500">
                  <tr>
                    <th className="px-2 py-1">template cell</th>
                    <th className="px-2 py-1">← source</th>
                    <th className="px-2 py-1">value</th>
                    <th className="px-2 py-1">metric</th>
                    <th className="px-2 py-1">period · scenario</th>
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
            <p className="text-sm text-neutral-500">Nothing matched — check the source has the metrics this template needs.</p>
          )}
        </div>
      ) : null}
    </section>
  );
}

export default function TemplatePage() {
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<Understanding | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [ready, setReady] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await getUnderstanding(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setReady(true);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleRun() {
    setRunning(true);
    setError(null);
    try {
      await understandTemplate(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Understanding failed");
    } finally {
      setRunning(false);
    }
  }

  const wb = data?.workbook;
  const inputsBySheet = (data?.critical_inputs ?? []).reduce<Record<string, CriticalInput[]>>(
    (acc, ci) => {
      (acc[ci.sheet_name] ??= []).push(ci);
      return acc;
    },
    {}
  );
  // Sheets with content first (input surface, then those with captured inputs).
  const sheets = [...(data?.sheets ?? [])].sort((a, b) => {
    const aw = (inputsBySheet[a.sheet_name]?.length ?? 0) > 0 ? 0 : 1;
    const bw = (inputsBySheet[b.sheet_name]?.length ?? 0) > 0 ? 0 : 1;
    return aw - bw;
  });

  return (
    <div>
      <Link href="/" className="text-sm text-neutral-500 transition hover:text-neutral-800">
        ← Template library
      </Link>

      {error ? (
        <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-600">{error}</p>
      ) : null}

      {!ready ? (
        <p className="mt-8 text-sm text-neutral-400">Loading…</p>
      ) : !data?.available ? (
        <div className="mt-8 flex flex-col items-center justify-center rounded-xl border border-dashed border-neutral-300 bg-white px-6 py-16 text-center">
          <h2 className="text-base font-medium">Not analysed yet</h2>
          <p className="mt-1 max-w-md text-sm text-neutral-500">
            Run the understanding pass to extract this template’s purpose, its critical input
            areas, and snippets of where the portfolio company fills in data. Takes a few minutes.
          </p>
          <button
            onClick={handleRun}
            disabled={running}
            className="mt-5 rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-neutral-700 disabled:opacity-50"
          >
            {running ? "Analysing… (a few minutes)" : "Understand template"}
          </button>
        </div>
      ) : (
        <div className="mt-4 space-y-6">
          <header>
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-semibold tracking-tight">
                {wb?.archetype ?? "Template understanding"}
              </h1>
              <button
                onClick={handleRun}
                disabled={running}
                className="ml-auto rounded-md border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-700 transition hover:bg-neutral-50 disabled:opacity-50"
              >
                {running ? "Re-analysing…" : "Re-run"}
              </button>
            </div>
            {wb?.purpose ? <p className="mt-1 text-sm text-neutral-600">{wb.purpose}</p> : null}
            {wb?.audience ? (
              <p className="mt-0.5 text-xs text-neutral-400">Audience: {wb.audience}</p>
            ) : null}
            {wb?.summary ? (
              <p className="mt-3 max-w-3xl text-sm leading-relaxed text-neutral-700">{wb.summary}</p>
            ) : null}
            {wb?.input_surface_sheets?.length ? (
              <p className="mt-3 text-xs text-neutral-500">
                <span className="font-medium text-neutral-600">Input sheets: </span>
                {wb.input_surface_sheets.join(", ")}
              </p>
            ) : null}
          </header>

          <PopulatePanel templateId={id} />

          {wb?.review_flags?.length ? (
            <section className="rounded-xl border border-amber-200 bg-amber-50/50 p-4">
              <h2 className="text-sm font-semibold text-amber-800">Needs human review</h2>
              <ul className="mt-2 space-y-1.5">
                {wb.review_flags.map((f, i) => (
                  <li key={i} className="text-xs text-amber-700">
                    • {f}
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <div>
            <h2 className="mb-3 text-lg font-medium">Critical input areas</h2>
            <div className="space-y-4">
              {sheets.map((s) => (
                <SheetPanel key={s.id} sheet={s} inputs={inputsBySheet[s.sheet_name] ?? []} />
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
