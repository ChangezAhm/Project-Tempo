import { NextResponse } from "next/server";

// Downloads a filled workbook (signed Supabase storage URL) on behalf of the
// task pane, so the pane never depends on storage CORS. Restricted to our own
// Supabase project to keep this from being an open proxy.
const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";

export async function GET(req: Request) {
  const url = new URL(req.url).searchParams.get("url");
  if (!url || !SUPABASE_URL || !url.startsWith(SUPABASE_URL)) {
    return NextResponse.json({ error: "Invalid file URL" }, { status: 400 });
  }
  const upstream = await fetch(url, { cache: "no-store" });
  if (!upstream.ok || !upstream.body) {
    return NextResponse.json(
      { error: `Storage returned ${upstream.status}` },
      { status: 502 }
    );
  }
  return new NextResponse(upstream.body, {
    headers: {
      "Content-Type":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "Cache-Control": "no-store",
    },
  });
}
