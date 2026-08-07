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
import { Badge, Button, EmptyState, ErrorNote, LinkButton } from "@/app/components/ui";

// Placeholder art for templates that have not been onboarded yet — a quiet
// spreadsheet grid, no screenshot to show.
function GridPlaceholder() {
  return (
    <div className="flex h-full w-full items-center justify-center bg-neutral-100">
      <svg width="72" height="48" viewBox="0 0 72 48" fill="none" aria-hidden>
        {[0, 12, 24, 36, 48, 60, 72].map((x) => (
          <line key={`v${x}`} x1={x} y1="0" x2={x} y2="48" stroke="var(--color-neutral-300)" strokeWidth="1" />
        ))}
        {[0, 12, 24, 36, 48].map((y) => (
          <line key={`h${y}`} x1="0" y1={y} x2="72" y2={y} stroke="var(--color-neutral-300)" strokeWidth="1" />
        ))}
        <rect x="0" y="0" width="24" height="12" fill="var(--color-neutral-200)" />
      </svg>
    </div>
  );
}

function TemplateCard({
  t,
  index,
  analyzing,
  parseSummary,
  onAnalyze,
  onDeleteRequest,
  confirming,
  deleting,
  onDeleteConfirm,
  onDeleteCancel,
}: {
  t: Template;
  index: number;
  analyzing: boolean;
  parseSummary: ParseSummary | undefined;
  onAnalyze: () => void;
  onDeleteRequest: () => void;
  confirming: boolean;
  deleting: boolean;
  onDeleteConfirm: () => void;
  onDeleteCancel: () => void;
}) {
  return (
    <li
      className="group panel animate-fade-up relative flex flex-col overflow-hidden transition duration-300 hover:shadow-lift"
      style={{ animationDelay: `${Math.min(index, 8) * 60}ms` }}
    >
      <Link href={`/template/${t.id}`} className="block">
        <div className="relative h-40 w-full overflow-hidden border-b border-neutral-200 bg-neutral-100">
          {t.thumbnailUrl ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={t.thumbnailUrl}
              alt={`${t.name} — sheet preview`}
              className="h-full w-full object-cover object-left-top transition duration-500 group-hover:scale-[1.025]"
            />
          ) : (
            <GridPlaceholder />
          )}
          <div className="absolute left-3 top-3">
            {t.understood ? (
              <Badge tone="green" className="bg-white/90 shadow-sm backdrop-blur">
                Onboarded · {t.understoodSheetCount} sheet{t.understoodSheetCount === 1 ? "" : "s"}
              </Badge>
            ) : (
              <Badge className="bg-white/90 shadow-sm backdrop-blur">Awaiting onboarding</Badge>
            )}
          </div>
        </div>
      </Link>

      <button
        onClick={onDeleteRequest}
        className="absolute right-2.5 top-2.5 rounded-md bg-white/85 p-1.5 text-neutral-400 opacity-0 shadow-sm backdrop-blur transition hover:text-red-700 group-hover:opacity-100"
        aria-label={`Delete ${t.name}`}
        title="Delete template"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M3 6h18" />
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
        </svg>
      </button>

      <div className="flex flex-1 flex-col p-4">
        <Link href={`/template/${t.id}`} className="block">
          <h3
            className="truncate text-[1.05rem] font-semibold tracking-tight"
            style={{ fontFamily: "var(--font-display)" }}
            title={t.name}
          >
            {t.name}
          </h3>
        </Link>
        <p className="mt-0.5 truncate text-xs text-neutral-400" title={t.archetype ?? t.fileName}>
          {t.archetype ?? t.fileName}
        </p>

        <div className="mt-auto pt-4">
          <div className="flex items-center justify-between text-[11px] text-neutral-400">
            <span>
              {formatSize(t.sizeBytes)} · {new Date(t.uploadedAt).toLocaleDateString()}
            </span>
            {parseSummary ? (
              <span>
                {parseSummary.sheet_count} sheets · {parseSummary.total_formulas.toLocaleString()} formulas
              </span>
            ) : (
              <button
                onClick={onAnalyze}
                disabled={analyzing}
                className="font-medium text-neutral-400 transition hover:text-ink disabled:opacity-50"
              >
                {analyzing ? "Analyzing…" : "Analyze structure"}
              </button>
            )}
          </div>
          <LinkButton href={`/template/${t.id}`} variant="primary" size="sm" className="mt-3 w-full">
            Open
          </LinkButton>
        </div>
      </div>

      {confirming ? (
        <div className="animate-fade-in absolute inset-0 z-10 flex flex-col items-center justify-center gap-3 bg-white/95 p-4 text-center backdrop-blur-sm">
          <p className="text-sm font-medium">Delete this template?</p>
          <p className="max-w-full truncate text-xs text-neutral-500" title={t.name}>
            {t.name}
          </p>
          <div className="flex gap-2">
            <Button variant="danger" onClick={onDeleteConfirm} disabled={deleting}>
              {deleting ? "Deleting…" : "Delete"}
            </Button>
            <Button variant="secondary" onClick={onDeleteCancel} disabled={deleting}>
              Cancel
            </Button>
          </div>
        </div>
      ) : null}
    </li>
  );
}

export default function LibraryPage() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  // Set of in-flight analyses — a single id would let two concurrent
  // Analyze clicks clobber each other's spinner/disabled state.
  const [analyzingIds, setAnalyzingIds] = useState<Set<string>>(new Set());
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
    setAnalyzingIds((ids) => new Set(ids).add(id));
    setError(null);
    try {
      const summary = await parseTemplate(id);
      setResults((r) => ({ ...r, [id]: summary }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to analyze template");
    } finally {
      setAnalyzingIds((ids) => {
        const next = new Set(ids);
        next.delete(id);
        return next;
      });
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
      <div className="mb-10 flex flex-wrap items-end gap-3">
        <div>
          <p className="mb-1 text-[11px] font-medium uppercase tracking-[0.16em] text-neutral-400">
            Library
          </p>
          <h1 className="text-[2rem] leading-tight">Templates</h1>
        </div>
        {templates.length > 0 ? (
          <p className="ml-auto text-sm text-neutral-400">
            {templates.length} template{templates.length === 1 ? "" : "s"}
          </p>
        ) : null}
      </div>

      {error ? <div className="mb-6"><ErrorNote>{error}</ErrorNote></div> : null}

      {!ready ? (
        <ul className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <li key={i} className="panel overflow-hidden">
              <div className="shimmer h-40 w-full" />
              <div className="space-y-2 p-4">
                <div className="shimmer h-4 w-2/3 rounded" />
                <div className="shimmer h-3 w-1/2 rounded" />
              </div>
            </li>
          ))}
        </ul>
      ) : templates.length === 0 ? (
        <EmptyState
          title="No templates yet"
          body="Upload a sponsor Excel template and Tempo will read it, map its structure and build its contract."
        >
          <LinkButton href="/upload" variant="primary" size="md">
            Upload a template
          </LinkButton>
        </EmptyState>
      ) : (
        <ul className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {templates.map((t, i) => (
            <TemplateCard
              key={t.id}
              t={t}
              index={i}
              analyzing={analyzingIds.has(t.id)}
              parseSummary={results[t.id]}
              onAnalyze={() => handleAnalyze(t.id)}
              onDeleteRequest={() => setConfirmId(t.id)}
              confirming={confirmId === t.id}
              deleting={deletingId === t.id}
              onDeleteConfirm={() => handleDelete(t.id)}
              onDeleteCancel={() => setConfirmId(null)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
