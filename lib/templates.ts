// Durable template store for the MVP (Build Step 1).
// Uploads the raw workbook to Supabase Storage and records the
// templates -> template_versions -> template_files hierarchy in Postgres.
// See docs/Migration-Plan.md §5 "Build Step 1" and §9 schema.
//
// All Supabase access is server-mediated: the browser calls the Next.js
// routes under /api/v1, which use the service-role key (see
// utils/supabase/admin.ts). RLS is locked down accordingly in
// supabase/migrations/0007_lock_rls.sql.

export type Template = {
  id: string;
  name: string;
  sponsorName: string | null;
  note: string | null;
  fileName: string;
  sizeBytes: number;
  uploadedAt: string; // ISO string
};

export async function getTemplates(): Promise<Template[]> {
  const res = await fetch("/api/v1/templates", { cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to load templates (${res.status})`);
  }
  return body as Template[];
}

// Stores the workbook durably and creates template + version + file rows.
// Returns the new template_id.
export async function uploadTemplate(input: {
  file: File;
  name: string;
  note?: string;
  sponsorName?: string;
}): Promise<string> {
  const form = new FormData();
  form.append("file", input.file);
  form.append("name", input.name);
  if (input.note) form.append("note", input.note);
  if (input.sponsorName) form.append("sponsorName", input.sponsorName);

  const res = await fetch("/api/v1/templates", { method: "POST", body: form });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Upload failed (${res.status})`);
  }
  return body.id as string;
}

export async function deleteTemplate(id: string): Promise<void> {
  const res = await fetch(`/api/v1/template/${id}`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error ?? `Delete failed (${res.status})`);
  }
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Structural parse summary returned by the Python parser service
// (via the /api/v1/template/[id]/parse route).
export type ParseSummary = {
  job_id: string;
  template_version_id: string;
  filename: string;
  sheet_count: number;
  hidden_sheet_count: number;
  total_cells: number;
  total_formulas: number;
  total_named_ranges: number;
  has_vba: boolean;
  sheets: {
    name: string;
    index: number;
    is_hidden: boolean;
    is_protected: boolean;
    cell_count: number;
    used_range: string;
  }[];
};

// Kicks off structural extraction for a stored template. The Python service
// reads the workbook from Storage and persists template_sheets / analysis_jobs.
export async function parseTemplate(id: string): Promise<ParseSummary> {
  const res = await fetch(`/api/v1/template/${id}/parse`, { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Parse failed (${res.status})`);
  }
  return body as ParseSummary;
}

// --- Layer 3 understanding -------------------------------------------------

export type CriticalInput = {
  id: string;
  sheet_name: string;
  label: string;
  cells: string[];
  definition: string | null;
  qualification_criteria: string | null;
  expected_source: string | null;
  interpretation_source: string | null;
  unit: string | null;
  needs_value: boolean;
  rank: number;
  snippet_url: string | null;
};

export type SheetUnderstanding = {
  id: string;
  sheet_name: string;
  role: string | null;
  summary: string | null;
  snippet_url: string | null;
};

export type WorkbookUnderstanding = {
  archetype: string | null;
  purpose: string | null;
  audience: string | null;
  summary: string | null;
  input_surface_sheets: string[];
  review_flags: string[];
};

export type Understanding = {
  template_version_id: string;
  available: boolean;
  workbook?: WorkbookUnderstanding;
  sheets?: SheetUnderstanding[];
  critical_inputs?: CriticalInput[];
};

export type UnderstandSummary = {
  template_version_id: string;
  deep_sheets: string[];
  sheet_count: number;
  critical_input_count: number;
};

// Runs the LLM understanding (long-running, ~minutes) and persists it.
export async function understandTemplate(id: string): Promise<UnderstandSummary> {
  const res = await fetch(`/api/v1/template/${id}/understand`, { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Understanding failed (${res.status})`);
  }
  return body as UnderstandSummary;
}

// Reads the persisted understanding (summary + critical inputs + snippets).
export async function getUnderstanding(id: string): Promise<Understanding> {
  const res = await fetch(`/api/v1/template/${id}/understanding`, { cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to load understanding (${res.status})`);
  }
  return body as Understanding;
}

// --- Population -------------------------------------------------------------

export type FilledCell = {
  template_sheet: string;
  template_cell: string;
  value: number | string;
  raw_source_value: number | string;
  source_sheet: string;
  source_cell: string;
  metric: string;
  period_index: number | null;
  scenario: string | null;
  confidence: number;
};

export type PopulateResult = {
  target_template_id: string;
  source_filename: string;
  as_of_date: string | null;
  demand_metrics: number;
  summary: { facts: number; filled: number; unmatched: number; skipped: number };
  routing: Record<string, string[]> | null;
  links_count: number;
  filled: FilledCell[];
  filled_truncated: boolean;
  unmatched: { reason: string; template_sheet?: string; template_cell?: string; metric?: string }[];
  unmatched_count: number;
  skipped: { template_sheet: string; template_cell: string; reason: string }[];
  skipped_count: number;
  cleared_count: number;
  notes: string[];
  filled_url: string | null;
  audit_url: string | null;
};

// Fills `targetId` directly from a dropped data file. The file is parsed in
// memory by the parser and never stored as a template. Long-running (LLM).
// Returns the mapping, attribution + a download URL.
export type PopulateOptions = {
  asOf?: string | null;
  targetCurrency?: string | null;
  fxRate?: number | null;
};

export async function populateTemplate(
  targetId: string,
  file: File,
  opts: PopulateOptions = {}
): Promise<PopulateResult> {
  const form = new FormData();
  form.append("file", file);
  if (opts.asOf) form.append("as_of_date", opts.asOf);
  if (opts.targetCurrency) form.append("target_currency", opts.targetCurrency);
  if (opts.fxRate != null && Number.isFinite(opts.fxRate)) form.append("fx_rate", String(opts.fxRate));
  const res = await fetch(`/api/v1/template/${targetId}/populate`, {
    method: "POST",
    body: form,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Population failed (${res.status})`);
  }
  return body as PopulateResult;
}
