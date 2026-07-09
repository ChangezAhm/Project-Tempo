"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getTimeseries, type TimeseriesPeriod, type TimeseriesSheet, type TimeseriesView } from "@/lib/templates";

const SCENARIO_LABEL: Record<string, string> = {
  actual: "Actual",
  budget: "Budget",
  forecast: "Forecast",
};

// Relative-timeline sheets store no real dates, so their period labels can be
// formula-ish junk ("Jan-25 (AsOfDate-17)", "=EOMONTH…"). Fall back to Pn.
function periodLabel(p: TimeseriesPeriod): string {
  const l = (p.label ?? "").trim();
  if (!l || l.startsWith("=") || l.includes("AsOfDate") || l.includes("(")) {
    return p.index != null ? `P${p.index + 1}` : (p.key ?? "?");
  }
  return l;
}

function SheetGrid({ sheet, scenario }: { sheet: TimeseriesSheet; scenario: string }) {
  const periods = sheet.periods ?? [];
  const metrics = sheet.metrics ?? [];
  const hasScenario = (sheet.scenarios ?? []).includes(scenario);
  return (
    <section className="rounded-xl border border-neutral-200 bg-white">
      <div className="flex flex-wrap items-center gap-2 border-b border-neutral-200 px-4 py-3">
        <h3 className="font-medium">{sheet.sheet}</h3>
        <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-[10px] font-medium text-neutral-500">
          {sheet.grain}
        </span>
        {!sheet.is_timeseries ? (
          <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-[10px] font-medium text-neutral-400">
            single period
          </span>
        ) : null}
        <span className="ml-auto text-xs text-neutral-400">
          {metrics.length} metric{metrics.length === 1 ? "" : "s"} · {periods.length} period
          {periods.length === 1 ? "" : "s"}
        </span>
      </div>

      {!hasScenario ? (
        <p className="px-4 py-6 text-sm text-neutral-400">
          No {SCENARIO_LABEL[scenario] ?? scenario} figures on this tab.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left text-xs">
            <thead>
              <tr className="text-neutral-500">
                <th className="sticky left-0 z-10 min-w-[200px] border-b border-neutral-200 bg-white px-3 py-2 font-medium">
                  Metric
                </th>
                {periods.map((p) => (
                  <th
                    key={p.key}
                    title={p.date ?? p.label}
                    className="whitespace-nowrap border-b border-neutral-200 px-3 py-2 text-right font-medium"
                  >
                    {periodLabel(p)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {metrics.map((m) => {
                const row = m.cells[scenario] ?? {};
                const computed = m.category === "computed";
                return (
                  <tr key={m.label} className="border-b border-neutral-100 last:border-0 hover:bg-neutral-50/60">
                    <th
                      scope="row"
                      title={m.definition ?? undefined}
                      className={`sticky left-0 z-10 min-w-[200px] bg-white px-3 py-1.5 text-left font-normal ${
                        computed ? "text-neutral-400 italic" : "text-neutral-800"
                      }`}
                    >
                      <span className="align-middle">{m.label}</span>
                      {m.unit ? <span className="ml-1.5 text-[10px] text-neutral-400">{m.unit}</span> : null}
                      {computed ? (
                        <span className="ml-1.5 rounded bg-neutral-100 px-1 py-0.5 text-[9px] text-neutral-400">
                          computed
                        </span>
                      ) : null}
                    </th>
                    {periods.map((p) => {
                      const cell = row[p.key];
                      return (
                        <td key={p.key} className="px-3 py-1.5 text-right">
                          {cell ? (
                            <span className="font-mono text-[10px] text-neutral-300" title={`${sheet.sheet}!${cell}`}>
                              {cell}
                            </span>
                          ) : (
                            <span className="text-neutral-200">·</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function TimeseriesPage() {
  const { id } = useParams<{ id: string }>();
  const [view, setView] = useState<TimeseriesView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [scenario, setScenario] = useState<string>("actual");

  const load = useCallback(async () => {
    try {
      const v = await getTimeseries(id);
      setView(v);
      setError(null);
      const scen = v.scenarios ?? [];
      if (scen.length && !scen.includes(scenario)) setScenario(scen[0]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load");
    } finally {
      setReady(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const scenarios = useMemo(() => view?.scenarios ?? [], [view]);

  return (
    <div>
      <Link
        href={`/template/${id}`}
        className="text-sm text-neutral-500 transition hover:text-neutral-800"
      >
        ← Template
      </Link>

      <header className="mt-4 flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Data model — time series</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Every metric laid out across its periods, split by tab. Each slot is the template cell that
            period maps to.
          </p>
        </div>
        {scenarios.length > 1 ? (
          <div className="ml-auto inline-flex rounded-lg border border-neutral-300 p-0.5">
            {scenarios.map((s) => (
              <button
                key={s}
                onClick={() => setScenario(s)}
                className={`rounded-md px-3 py-1 text-xs font-medium transition ${
                  scenario === s
                    ? "bg-neutral-900 text-white"
                    : "text-neutral-600 hover:bg-neutral-100"
                }`}
              >
                {SCENARIO_LABEL[s] ?? s}
              </button>
            ))}
          </div>
        ) : scenarios.length === 1 ? (
          <span className="ml-auto rounded-full bg-neutral-100 px-3 py-1 text-xs font-medium text-neutral-500">
            {SCENARIO_LABEL[scenarios[0]] ?? scenarios[0]} only
          </span>
        ) : null}
      </header>

      {error ? (
        <div className="mt-8 rounded-xl border border-dashed border-red-200 bg-white px-6 py-12 text-center">
          <p className="text-sm text-neutral-600">Couldn’t load the time series.</p>
          <p className="mt-1 text-xs text-neutral-400">{error}</p>
          <button
            onClick={() => {
              setReady(false);
              void load();
            }}
            className="mt-4 rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-neutral-700"
          >
            Retry
          </button>
        </div>
      ) : !ready ? (
        <p className="mt-8 text-sm text-neutral-400">Loading…</p>
      ) : !view?.available ? (
        <div className="mt-8 rounded-xl border border-dashed border-neutral-300 bg-white px-6 py-12 text-center">
          <p className="text-sm text-neutral-500">
            No data model yet — run Understand on this template first.
          </p>
        </div>
      ) : (
        <div className="mt-6 space-y-5">
          {view.sheets.map((sheet) => (
            <SheetGrid key={sheet.sheet} sheet={sheet} scenario={scenario} />
          ))}
        </div>
      )}
    </div>
  );
}
