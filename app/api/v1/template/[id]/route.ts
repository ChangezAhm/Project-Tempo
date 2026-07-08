import { NextResponse } from "next/server";
import { createAdminClient } from "@/utils/supabase/admin";
import type { SupabaseClient } from "@supabase/supabase-js";

// DELETE /api/v1/template/[id] — removes the template rows (cascade) and
// best-effort cleans up every storage bucket the parser writes for it.
//
// Bucket layout (see parser/app/supabase_client.py — read-only reference):
//   template-files      {template_id}/{version_id}/{filename}   (template_files.storage_path)
//   template-snapshots  {version_id}.json.gz
//   template-snippets   {version_id}/{sheet}.png
//   template-filled     {version_id}/{source}.xlsx + {version_id}/{source}.audit.json

async function removeObjects(
  supabase: SupabaseClient,
  bucket: string,
  paths: string[],
  templateId: string
): Promise<void> {
  if (paths.length === 0) return;
  const { error } = await supabase.storage.from(bucket).remove(paths);
  if (error) {
    console.error(
      `[delete-template ${templateId}] failed to remove ${paths.length} object(s) from ${bucket}: ${error.message}`
    );
  }
}

export async function DELETE(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const supabase = createAdminClient();

  // Version ids first — snapshot/snippet/filled objects are keyed by version.
  const { data: versions, error: verErr } = await supabase
    .from("template_versions")
    .select("id")
    .eq("template_id", id);
  if (verErr) {
    return NextResponse.json({ error: verErr.message }, { status: 500 });
  }
  const versionIds = ((versions as { id: string }[] | null) ?? []).map((v) => v.id);

  // template-files: exact paths are recorded on the file rows.
  const { data: files, error: filesErr } = await supabase
    .from("template_files")
    .select("storage_path, template_versions!inner(template_id)")
    .eq("template_versions.template_id", id);
  if (filesErr) {
    console.error(
      `[delete-template ${id}] failed to list template_files rows: ${filesErr.message}`
    );
  }
  const filePaths = ((files as { storage_path: string }[] | null) ?? []).map(
    (f) => f.storage_path
  );
  await removeObjects(supabase, "template-files", filePaths, id);

  // template-snapshots: one deterministic object per version.
  await removeObjects(
    supabase,
    "template-snapshots",
    versionIds.map((vid) => `${vid}.json.gz`),
    id
  );

  // template-snippets / template-filled: list each version's folder, then remove.
  for (const bucket of ["template-snippets", "template-filled"]) {
    const paths: string[] = [];
    for (const vid of versionIds) {
      const { data: objects, error: listErr } = await supabase.storage
        .from(bucket)
        .list(vid);
      if (listErr) {
        console.error(
          `[delete-template ${id}] failed to list ${bucket}/${vid}: ${listErr.message}`
        );
        continue;
      }
      for (const obj of objects ?? []) paths.push(`${vid}/${obj.name}`);
    }
    await removeObjects(supabase, bucket, paths, id);
  }

  // Finally delete the template row; versions/files/analysis rows cascade.
  const { error } = await supabase.from("templates").delete().eq("id", id);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  return NextResponse.json({ ok: true });
}
