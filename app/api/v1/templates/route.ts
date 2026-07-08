import { NextResponse } from "next/server";
import { createAdminClient } from "@/utils/supabase/admin";

// Server-mediated template store. The browser no longer talks to Supabase
// directly (see lib/templates.ts); these routes run with the service-role
// key so RLS can be locked down (supabase/migrations/0007_lock_rls.sql).

const BUCKET = "template-files";

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
      "id, name, sponsor_name, note, created_at, template_versions(version_number, template_files(original_filename, size_bytes, created_at))"
    )
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const templates = ((data as RawTemplateRow[] | null) ?? []).map((row) => {
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
