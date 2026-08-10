import { NextResponse } from "next/server";

// Server-side only. Reads the persisted Layer-3 understanding for the UI.
const PARSER_URL = process.env.PARSER_SERVICE_URL ?? "http://localhost:8000";
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  try {
    const res = await fetch(`${PARSER_URL}/understanding/${id}`, {
      headers: PARSER_API_KEY ? { "X-API-Key": PARSER_API_KEY } : {},
      cache: "no-store",
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const message = body?.detail ?? body?.error ?? `Parser returned ${res.status}`;
      return NextResponse.json({ error: message }, { status: res.status });
    }
    // Rewrite parser-signed snippet URLs (fresh token each request = browser
    // cache busted every load) to the stable, downscaling /api/v1/snippet
    // proxy so sheet screenshots cache like normal images.
    const proxied = JSON.parse(
      JSON.stringify(body).replace(
        /"(https?:[^"]*?\/template-snippets\/([^"?]+))(\?[^"]*)?"/g,
        (_m, _full, path) =>
          JSON.stringify(
            `/api/v1/snippet?path=${encodeURIComponent(decodeURIComponent(path))}&w=1400`
          )
      )
    );
    return NextResponse.json(proxied);
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? `Parser unreachable: ${e.message}` : "Parser unreachable" },
      { status: 502 }
    );
  }
}
