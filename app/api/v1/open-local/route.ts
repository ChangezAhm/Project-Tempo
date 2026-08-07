import { exec } from "node:child_process";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { NextResponse } from "next/server";

// Local-dev superpower: the Next server runs on the SAME machine as Excel, so
// "take me to the filled file" can be literal — download the signed storage
// URL to a temp file and shell-open it in Excel natively. Used as the fallback
// when Excel.createWorkbook (the in-pane path) rejects a large payload.
// Pinned to our Supabase project; Windows/mac only, no-ops elsewhere.
const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";

export async function POST(req: Request) {
  const { url, name } = (await req.json().catch(() => ({}))) as {
    url?: string;
    name?: string;
  };
  if (!url || !SUPABASE_URL || !url.startsWith(SUPABASE_URL)) {
    return NextResponse.json({ error: "Invalid file URL" }, { status: 400 });
  }
  if (process.platform !== "win32" && process.platform !== "darwin") {
    return NextResponse.json({ error: "Local open unsupported here" }, { status: 501 });
  }
  const upstream = await fetch(url, { cache: "no-store" });
  if (!upstream.ok) {
    return NextResponse.json(
      { error: `Storage returned ${upstream.status}` },
      { status: 502 }
    );
  }
  const buf = Buffer.from(await upstream.arrayBuffer());
  const safe = (name ?? "filled.xlsx").replace(/[^\w.\- ]+/g, "_");
  const dir = await mkdtemp(path.join(tmpdir(), "tempo-filled-"));
  const file = path.join(dir, safe.endsWith(".xlsx") ? safe : `${safe}.xlsx`);
  await writeFile(file, buf);
  await new Promise<void>((resolve, reject) => {
    const cmd =
      process.platform === "win32"
        ? `cmd /c start "" "${file}"`
        : `open "${file}"`;
    exec(cmd, (err) => (err ? reject(err) : resolve()));
  }).catch((e) => {
    throw e;
  });
  return NextResponse.json({ opened: file });
}
