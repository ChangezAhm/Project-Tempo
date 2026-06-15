// Durable template store for the MVP (Build Step 1).
// Uploads the raw workbook to Supabase Storage and records the
// templates -> template_versions -> template_files hierarchy in Postgres.
// See docs/Migration-Plan.md §5 "Build Step 1" and §9 schema.
//
// We talk to Supabase directly from the browser using the publishable (anon)
// key. There is no auth yet, so RLS is permissive (see supabase/migrations).
// A server route (/api/v1/template/upload) can wrap this later once a
// service-role key + auth exist; the upload flow stays the same.

import { createClient } from "@/utils/supabase/client";

const BUCKET = "template-files";

export type Template = {
  id: string;
  name: string;
  sponsorName: string | null;
  note: string | null;
  fileName: string;
  sizeBytes: number;
  uploadedAt: string; // ISO string
};

// Shape of the nested select we read back from Supabase.
type RawTemplateRow = {
  id: string;
  name: string;
  sponsor_name: string | null;
  note: string | null;
  created_at: string;
  template_versions: {
    version_number: number;
    template_files: {
      original_filename: string;
      size_bytes: number;
      created_at: string;
    }[];
  }[];
};

function sanitizeKey(name: string): string {
  return name.replace(/[^a-zA-Z0-9._-]/g, "_");
}

async function sha256Hex(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function getTemplates(): Promise<Template[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from("templates")
    .select(
      "id, name, sponsor_name, note, created_at, template_versions(version_number, template_files(original_filename, size_bytes, created_at))"
    )
    .order("created_at", { ascending: false });

  if (error) throw new Error(error.message);

  return ((data as RawTemplateRow[] | null) ?? []).map((row) => {
    const latestVersion = [...row.template_versions].sort(
      (a, b) => b.version_number - a.version_number
    )[0];
    const file = latestVersion?.template_files?.[0];
    return {
      id: row.id,
      name: row.name,
      sponsorName: row.sponsor_name,
      note: row.note,
      fileName: file?.original_filename ?? "—",
      sizeBytes: file?.size_bytes ?? 0,
      uploadedAt: file?.created_at ?? row.created_at,
    };
  });
}

// Stores the workbook durably and creates template + version + file rows.
// Returns the new template_id.
export async function uploadTemplate(input: {
  file: File;
  name: string;
  note?: string;
  sponsorName?: string;
}): Promise<string> {
  const supabase = createClient();
  const { file } = input;

  // 1. Template row
  const { data: tmpl, error: tmplErr } = await supabase
    .from("templates")
    .insert({
      name: input.name,
      sponsor_name: input.sponsorName ?? null,
      note: input.note ?? null,
    })
    .select("id")
    .single();
  if (tmplErr || !tmpl) {
    throw new Error(tmplErr?.message ?? "Failed to create template");
  }

  try {
    // 2. Version row (v1)
    const { data: version, error: verErr } = await supabase
      .from("template_versions")
      .insert({ template_id: tmpl.id, version_number: 1 })
      .select("id")
      .single();
    if (verErr || !version) {
      throw new Error(verErr?.message ?? "Failed to create version");
    }

    // 3. Upload the raw workbook to private storage
    const storagePath = `${tmpl.id}/${version.id}/${sanitizeKey(file.name)}`;
    const { error: upErr } = await supabase.storage
      .from(BUCKET)
      .upload(storagePath, file, {
        contentType: file.type || "application/octet-stream",
        upsert: false,
      });
    if (upErr) throw new Error(upErr.message);

    // 4. File row (with integrity hash)
    const sha256 = await sha256Hex(file);
    const { error: fileErr } = await supabase.from("template_files").insert({
      template_version_id: version.id,
      storage_path: storagePath,
      original_filename: file.name,
      content_type: file.type || null,
      size_bytes: file.size,
      sha256,
    });
    if (fileErr) throw new Error(fileErr.message);

    return tmpl.id as string;
  } catch (err) {
    // Best-effort rollback: cascade deletes the version + file rows.
    await supabase.from("templates").delete().eq("id", tmpl.id);
    throw err;
  }
}

export async function deleteTemplate(id: string): Promise<void> {
  const supabase = createClient();

  // Remove stored objects for this template before deleting the rows.
  const { data: files } = await supabase
    .from("template_files")
    .select("storage_path, template_versions!inner(template_id)")
    .eq("template_versions.template_id", id);

  const paths = ((files as { storage_path: string }[] | null) ?? []).map(
    (f) => f.storage_path
  );
  if (paths.length > 0) {
    await supabase.storage.from(BUCKET).remove(paths);
  }

  const { error } = await supabase.from("templates").delete().eq("id", id);
  if (error) throw new Error(error.message);
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
export async function populateTemplate(
  targetId: string,
  file: File,
  asOf: string | null
): Promise<PopulateResult> {
  const form = new FormData();
  form.append("file", file);
  if (asOf) form.append("as_of_date", asOf);
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
