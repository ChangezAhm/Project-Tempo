"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  deleteTemplate,
  formatSize,
  getTemplates,
  parseTemplate,
  type ParseSummary,
  type Template,
} from "@/lib/templates";

export default function LibraryPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [analyzingId, setAnalyzingId] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, ParseSummary>>({});

  useEffect(() => {
    let active = true;
    getTemplates()
      .then((t) => active && setTemplates(t))
      .catch((e) => active && setError(e instanceof Error ? e.message : "Failed to load templates"))
      .finally(() => active && setReady(true));
    return () => {
      active = false;
    };
  }, []);

  async function handleAnalyze(id: string) {
    setAnalyzingId(id);
    setError(null);
    try {
      const summary = await parseTemplate(id);
      setResults((r) => ({ ...r, [id]: summary }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to analyze template");
    } finally {
      setAnalyzingId(null);
    }
  }

  async function handleDelete(id: string) {
    setDeletingId(id);
    setError(null);
    try {
      await deleteTemplate(id);
      setTemplates(await getTemplates());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete template");
    } finally {
      setDeletingId(null);
      setConfirmId(null);
    }
  }

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Template Library</h1>
        <p className="mt-1 text-sm text-neutral-500">
          Sponsor reporting templates onboarded for contract creation.
        </p>
      </div>

      {error ? (
        <p className="mb-6 rounded-md bg-red-50 px-3 py-2 text-sm text-red-600">{error}</p>
      ) : null}

      {!ready ? null : templates.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-neutral-300 bg-white px-6 py-16 text-center">
          <div className="mb-3 inline-flex h-10 w-10 items-center justify-center rounded-lg bg-neutral-100 text-neutral-400">
            ⌬
          </div>
          <h2 className="text-base font-medium">No templates yet</h2>
          <p className="mt-1 max-w-sm text-sm text-neutral-500">
            Upload a sponsor Excel template to start building its Template Contract.
          </p>
          <Link
            href="/upload"
            className="mt-5 rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-neutral-700"
          >
            Upload template
          </Link>
        </div>
      ) : (
        <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {templates.map((t) => (
            <li
              key={t.id}
              className="group relative flex flex-col rounded-xl border border-neutral-200 bg-white p-4 transition hover:border-neutral-300 hover:shadow-sm"
            >
              <div className="mb-3 flex items-start justify-between">
                <div className="inline-flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-50 text-sm font-semibold text-emerald-600">
                  XLS
                </div>
                <button
                  onClick={() => setConfirmId(t.id)}
                  className="rounded-md p-1.5 text-neutral-400 transition hover:bg-red-50 hover:text-red-600"
                  aria-label={`Delete ${t.name}`}
                  title="Delete template"
                >
                  <svg
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M3 6h18" />
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                    <line x1="10" y1="11" x2="10" y2="17" />
                    <line x1="14" y1="11" x2="14" y2="17" />
                  </svg>
                </button>
              </div>
              <h3 className="truncate font-medium" title={t.name}>
                {t.name}
              </h3>
              <p className="mt-0.5 truncate text-xs text-neutral-400" title={t.fileName}>
                {t.fileName}
              </p>
              {t.note ? (
                <p className="mt-2 line-clamp-2 text-xs text-neutral-500">{t.note}</p>
              ) : null}
              <div className="mt-auto pt-4">
                <div className="flex items-center gap-2 text-xs text-neutral-400">
                  <span>{formatSize(t.sizeBytes)}</span>
                  <span>·</span>
                  <span>{new Date(t.uploadedAt).toLocaleDateString()}</span>
                </div>

                {results[t.id] ? (
                  <p className="mt-3 rounded-md bg-neutral-50 px-2.5 py-1.5 text-xs text-neutral-600">
                    {results[t.id].sheet_count} sheets ·{" "}
                    {results[t.id].total_cells.toLocaleString()} cells ·{" "}
                    {results[t.id].total_formulas.toLocaleString()} formulas
                  </p>
                ) : (
                  <button
                    onClick={() => handleAnalyze(t.id)}
                    disabled={analyzingId === t.id}
                    className="mt-3 w-full rounded-md border border-neutral-300 px-3 py-1.5 text-xs font-medium text-neutral-700 transition hover:bg-neutral-50 disabled:opacity-50"
                  >
                    {analyzingId === t.id ? "Analyzing…" : "Analyze structure"}
                  </button>
                )}

                <Link
                  href={`/template/${t.id}`}
                  className="mt-2 block w-full rounded-md bg-neutral-900 px-3 py-1.5 text-center text-xs font-medium text-white transition hover:bg-neutral-700"
                >
                  Understanding →
                </Link>
              </div>

              {confirmId === t.id ? (
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 rounded-xl bg-white/95 p-4 text-center backdrop-blur-sm">
                  <p className="text-sm font-medium">Delete this template?</p>
                  <p className="max-w-full truncate text-xs text-neutral-500" title={t.name}>
                    {t.name}
                  </p>
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleDelete(t.id)}
                      disabled={deletingId === t.id}
                      className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-red-700 disabled:opacity-50"
                    >
                      {deletingId === t.id ? "Deleting…" : "Delete"}
                    </button>
                    <button
                      onClick={() => setConfirmId(null)}
                      disabled={deletingId === t.id}
                      className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium text-neutral-700 transition hover:bg-neutral-50 disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
