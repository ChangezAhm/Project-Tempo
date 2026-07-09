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

export type ProposedAddition = {
  sheet_name: string;
  row: number;
  label: string;
  unit: string | null;
  kind: string | null;
  values: { col: number; source_sheet: string; source_cell: string }[];
};

export type AppliedAddition = {
  sheet_name: string;
  row: number;
  label: string;
  cells_written: number;
};

export type PopulateResult = {
  target_template_id: string;
  source_filename: string;
  as_of_date: string | null;
  demand_metrics: number;
  summary: { facts: number; filled: number; unmatched: number; skipped: number };
  routing: (Record<string, unknown> & { hint?: string }) | null;
  links_count: number;
  filled: FilledCell[];
  filled_truncated: boolean;
  unmatched: { reason: string; template_sheet?: string; template_cell?: string; metric?: string }[];
  unmatched_count: number;
  unmatched_reasons?: { reason: string; count: number }[];
  unmapped_metrics?: string[];
  skipped: { template_sheet: string; template_cell: string; reason: string }[];
  skipped_count: number;
  cleared_count: number;
  reset?: string;
  cleared_values?: number;
  cleared_formulas?: number;
  rule_violations?: {
    template_sheet: string;
    template_cell: string;
    metric: string;
    value: number;
    expected: string;
    rule: string;
  }[];
  rule_violation_count?: number;
  proposed_additions?: ProposedAddition[];
  additions_applied?: AppliedAddition[];
  notes: string[];
  filled_url: string | null;
  audit_url: string | null;
};

// Fills `targetId` directly from a dropped data file. The file is parsed in
// memory by the parser and never stored as a template. Long-running (LLM).
// Returns the mapping, attribution + a download URL.
export type PopulateOptions = {
  asOf?: string | null;
  reset?: "values" | "full";
  addLines?: "off" | "propose" | "apply";
};

export async function populateTemplate(
  targetId: string,
  file: File,
  opts: PopulateOptions = {}
): Promise<PopulateResult> {
  const form = new FormData();
  form.append("file", file);
  if (opts.asOf) form.append("as_of_date", opts.asOf);
  if (opts.reset) form.append("reset", opts.reset);
  if (opts.addLines) form.append("add_lines", opts.addLines);
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

// --- Template Contract review ------------------------------------------------

export type ContractCorrection = {
  id: string;
  match: Record<string, unknown>;
  patch: Record<string, unknown>;
  note: string | null;
  created_at?: string | null;
};

export type Contract = {
  template_version_id?: string;
  status: string; // "draft" | "approved"
  notes: string | null;
  corrections: ContractCorrection[];
};

export type ContractField = {
  sheet_name: string;
  metric_label: string;
  canonical_metric: string | null;
  unit: string | null;
  sign_convention: string | null;
  scenarios: string[];
  category_counts: Record<string, number>;
  fillable_count: number;
  fact_count: number;
  cells: string[];
  corrected: boolean;
};

// Reads the contract shell: status, notes and the stored corrections.
export async function getContract(id: string): Promise<Contract> {
  const res = await fetch(`/api/v1/template/${id}/contract`, { cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to load contract (${res.status})`);
  }
  return body as Contract;
}

// Reads the reviewable field grid (one row per sheet × metric label).
export async function getContractFields(id: string): Promise<{ fields: ContractField[] }> {
  const res = await fetch(`/api/v1/template/${id}/contract/fields`, { cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to load contract fields (${res.status})`);
  }
  return body as { fields: ContractField[] };
}

// Updates contract status ("draft" | "approved") and/or reviewer notes.
export async function patchContract(
  id: string,
  patch: { status?: string; notes?: string }
): Promise<Contract> {
  const res = await fetch(`/api/v1/template/${id}/contract`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to update contract (${res.status})`);
  }
  return body as Contract;
}

// Stores a persistent data-model correction. `match` selects facts
// (e.g. { sheet_name, metric_label }), `patch` sets the corrected values.
export async function addCorrection(
  id: string,
  input: { match: Record<string, unknown>; patch: Record<string, unknown>; note?: string }
): Promise<ContractCorrection> {
  const res = await fetch(`/api/v1/template/${id}/corrections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to add correction (${res.status})`);
  }
  return body as ContractCorrection;
}

export async function deleteCorrection(id: string, cid: string): Promise<void> {
  const res = await fetch(`/api/v1/template/${id}/corrections/${cid}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error ?? `Failed to delete correction (${res.status})`);
  }
}

// Re-runs the deterministic data-model derivation (re-applies corrections).
// Takes a few seconds.
export async function rederiveDataModel(id: string): Promise<Record<string, unknown>> {
  const res = await fetch(`/api/v1/template/${id}/datamodel`, { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Re-derive failed (${res.status})`);
  }
  return body as Record<string, unknown>;
}

// --- Data model as a time series ---------------------------------------------

export type TimeseriesPeriod = {
  key: string;
  date: string | null;
  index: number | null;
  label: string;
};

export type TimeseriesMetric = {
  metric: string;
  label: string;
  unit: string | null;
  basis: string | null;
  category: string; // data | sourced | computed
  definition: string | null;
  // scenario -> periodKey -> template cell (e.g. { actual: { "2026-01": "D9" } })
  cells: Record<string, Record<string, string>>;
};

export type TimeseriesSheet = {
  sheet: string;
  grain: string;
  is_timeseries: boolean;
  periods: TimeseriesPeriod[];
  scenarios: string[];
  metrics: TimeseriesMetric[];
};

export type TimeseriesView = {
  template_version_id: string;
  available: boolean;
  scenarios: string[]; // union across tabs, e.g. ["actual", "budget"]
  sheets: TimeseriesSheet[];
};

// Reads the data model laid out as a time series: per tab, metrics as rows and
// periods as columns, with an actual/budget/forecast dimension.
export async function getTimeseries(id: string): Promise<TimeseriesView> {
  const res = await fetch(`/api/v1/template/${id}/datamodel/timeseries`, { cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to load time series (${res.status})`);
  }
  return body as TimeseriesView;
}

// --- Review questions ---------------------------------------------------------

// One item in the template's review inbox. Generated by the understanding
// pass (and later by triage/derive/populate); machine-checkable items can be
// answered on demand by the deterministic dependency-graph verifier.
export type ReviewItem = {
  id: string;
  item_key: string;
  source: string; // understanding | triage | derive | populate
  kind: string; // machine_checkable | judgment | human_assisted | triage_decision
  question: string;
  why: string | null;
  affected: { sheets?: string[]; cells?: string[]; metrics?: string[] } | null;
  suggested_answer: string | null;
  check_spec: Record<string, unknown> | null;
  status: string; // open | verified | refuted | inconclusive | answered | dismissed
  resolution: {
    evidence?: Record<string, unknown>;
    answer?: string;
    reason?: string;
    resolved_at?: string;
    resolved_by?: string;
  } | null;
  created_at: string;
};

export type ReviewList = {
  template_version_id: string;
  count: number;
  open_count: number;
  items: ReviewItem[];
};

export type ReviewBuildResult = {
  template_version_id: string;
  added: number;
  count: number;
  note?: string;
};

// Reads the review inbox (all items + open count).
export async function getReviewItems(id: string): Promise<ReviewList> {
  const res = await fetch(`/api/v1/template/${id}/review`, { cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to load review questions (${res.status})`);
  }
  return body as ReviewList;
}

// Backfills review items from the stored understanding — for templates
// understood before the review inbox existed.
export async function buildReviewItems(id: string): Promise<ReviewBuildResult> {
  const res = await fetch(`/api/v1/template/${id}/review`, { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to build review questions (${res.status})`);
  }
  return body as ReviewBuildResult;
}

// Runs the deterministic dependency-graph check for one machine-checkable
// item. Status becomes verified | refuted | inconclusive, with proof in
// resolution.evidence.
export async function verifyReviewItem(id: string, itemId: string): Promise<ReviewItem> {
  const res = await fetch(`/api/v1/template/${id}/review/${itemId}/verify`, {
    method: "POST",
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body?.error) {
    throw new Error(body?.error ?? `Verification failed (${res.status})`);
  }
  return body as ReviewItem;
}

// Answers, dismisses or reopens a review item.
export async function answerReviewItem(
  id: string,
  itemId: string,
  patch: { status: "answered" | "dismissed" | "open"; answer?: string; reason?: string }
): Promise<ReviewItem> {
  const res = await fetch(`/api/v1/template/${id}/review/${itemId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to update review item (${res.status})`);
  }
  return body as ReviewItem;
}

// --- Extensible regions -------------------------------------------------------

export type ExtensibleRegion = {
  id?: string;
  sheet_name: string;
  kind: string | null;
  row_start: number;
  row_end: number;
  capacity: number | null;
  rules?: unknown;
};

function normalizeRegions(body: unknown): ExtensibleRegion[] {
  if (Array.isArray(body)) return body as ExtensibleRegion[];
  const regions = (body as { regions?: unknown } | null)?.regions;
  return Array.isArray(regions) ? (regions as ExtensibleRegion[]) : [];
}

// Reads stored extensible-region annotations (empty if never detected).
export async function getRegions(id: string): Promise<ExtensibleRegion[]> {
  const res = await fetch(`/api/v1/template/${id}/regions`, { cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Failed to load regions (${res.status})`);
  }
  return normalizeRegions(body);
}

// Runs LLM region detection on the parser (may take ~1 minute).
export async function detectRegions(id: string): Promise<ExtensibleRegion[]> {
  const res = await fetch(`/api/v1/template/${id}/regions`, { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body?.error ?? `Region detection failed (${res.status})`);
  }
  return normalizeRegions(body);
}
