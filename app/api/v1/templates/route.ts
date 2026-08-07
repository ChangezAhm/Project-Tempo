import { NextResponse } from "next/server";
import { createAdminClient } from "@/utils/supabase/admin";

// Server-mediated template store. The browser no longer talks to Supabase
// directly (see lib/templates.ts); these routes run with the service-role
// key so RLS can be locked down (supabase/migrations/0007_lock_rls.sql).

const BUCKET = "template-files";
// Sheet screenshots rendered by the understanding pass live here (private).
const SNIPPET_BUCKET = "template-snippets";

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
    // one-to-one embed (unique FK): PostgREST returns an object, but be
    // tolerant of the array shape too.
    template_understanding:
      | { archetype: string | null }
      | { archetype: string | null }[]
      | null;
    template_sheet_understanding: {
      sheet_name: string;
      role: string | null;
      snippet_path: string | null;
    }[];
  }[];
};

// Pick the sheet whose screenshot fronts the library card: the first input
// sheet with a snippet, falling back to any sheet with one.
function thumbnailPath(
  sheets: RawTemplateRow["template_versions"][number]["template_sheet_understanding"]
): string | null {
  const withSnippet = sheets.filter((s) => s.snippet_path);
  const input = withSnippet.find((s) => s.role === "input");
  return (input ?? withSnippet[0])?.snippet_path ?? null;
}

function sanitizeKey(name: string): string {
  return name.replace(/[^a-zA-Z0-9._-]/g, "_");
}

async function sha256Hex(buf: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// GET /api/v1/templates — list templates (latest version's file summary).
export async function GET() {
  const supabase = createAdminClient();
  const { data, error } = await supabase
    .from("templates")
    .select(
      "id, name, sponsor_name, note, created_at, template_versions(version_number, template_files(original_filename, size_bytes, created_at), template_understanding(archetype), template_sheet_understanding(sheet_name, role, snippet_path))"
    )
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const rows = ((data as RawTemplateRow[] | null) ?? []).map((row) => {
    const latestVersion = [...row.template_versions].sort(
      (a, b) => b.version_number - a.version_number
    )[0];
    const file = latestVersion?.template_files?.[0];
    const sheets = latestVersion?.template_sheet_understanding ?? [];
    return {
      id: row.id,
      name: row.name,
      sponsorName: row.sponsor_name,
      note: row.note,
      fileName: file?.original_filename ?? "—",
      sizeBytes: file?.size_bytes ?? 0,
      uploadedAt: file?.created_at ?? row.created_at,
      archetype: (() => {
        const u = latestVersion?.template_understanding;
        return (Array.isArray(u) ? u[0]?.archetype : u?.archetype) ?? null;
      })(),
      understood: sheets.length > 0,
      understoodSheetCount: sheets.length,
      snippetPath: thumbnailPath(sheets),
    };
  });

  // Sign every card thumbnail in one round trip (private bucket).
  const paths = [...new Set(rows.map((r) => r.snippetPath).filter((p): p is string => !!p))];
  const signed = new Map<string, string>();
  if (paths.length > 0) {
    const { data: urls } = await supabase.storage
      .from(SNIPPET_BUCKET)
      .createSignedUrls(paths, 3600);
    for (const u of urls ?? []) {
      if (u.path && u.signedUrl && !u.error) signed.set(u.path, u.signedUrl);
    }
  }

  const templates = rows.map(({ snippetPath, ...rest }) => ({
    ...rest,
    thumbnailUrl: snippetPath ? signed.get(snippetPath) ?? null : null,
  }));

  return NextResponse.json(templates);
}

// POST /api/v1/templates — multipart upload. Creates template + version +
// storage object + file row (mirrors the original lib/templates.ts flow,
// including the best-effort rollback).
export async function POST(req: Request) {
  const form = await req.formData().catch(() => null);
  const file = form?.get("file");
  const name = form?.get("name");
  if (!(file instanceof File) || typeof name !== "string" || !name) {
    return NextResponse.json(
      { error: "A file and a name are required" },
      { status: 400 }
    );
  }
  const note = form?.get("note");
  const sponsorName = form?.get("sponsorName");

  const supabase = createAdminClient();

  // 1. Template row
  const { data: tmpl, error: tmplErr } = await supabase
    .from("templates")
    .insert({
      name,
      sponsor_name: typeof sponsorName === "string" && sponsorName ? sponsorName : null,
      note: typeof note === "string" && note ? note : null,
    })
    .select("id")
    .single();
  if (tmplErr || !tmpl) {
    return NextResponse.json(
      { error: tmplErr?.message ?? "Failed to create template" },
      { status: 500 }
    );
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
    const bytes = await file.arrayBuffer();
    const storagePath = `${tmpl.id}/${version.id}/${sanitizeKey(file.name)}`;
    const { error: upErr } = await supabase.storage
      .from(BUCKET)
      .upload(storagePath, bytes, {
        contentType: file.type || "application/octet-stream",
        upsert: false,
      });
    if (upErr) throw new Error(upErr.message);

    // 4. File row (with integrity hash)
    const sha256 = await sha256Hex(bytes);
    const { error: fileErr } = await supabase.from("template_files").insert({
      template_version_id: version.id,
      storage_path: storagePath,
      original_filename: file.name,
      content_type: file.type || null,
      size_bytes: file.size,
      sha256,
    });
    if (fileErr) throw new Error(fileErr.message);

    return NextResponse.json({ id: tmpl.id as string });
  } catch (err) {
    // Best-effort rollback: cascade deletes the version + file rows.
    await supabase.from("templates").delete().eq("id", tmpl.id);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Upload failed" },
      { status: 500 }
    );
  }
}
