import { NextResponse } from "next/server";
import { parserFetch } from "@/lib/parserFetch";

// Server-side only. Kicks off the Layer-3 understanding run on the parser.
// This is long-running (multiple Opus calls, ~minutes) — there is no platform
// timeout in local dev. For hosted deployments this should move to a job/poll
// model; see the analysis_jobs row the parser writes.
const PARSER_URL = process.env.PARSER_SERVICE_URL ?? "http://localhost:8000";
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export const maxDuration = 800; // allow the long run on platforms that honour it

export async function POST(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  try {
    const res = await parserFetch(`${PARSER_URL}/understand/${id}`, {
      method: "POST",
      headers: PARSER_API_KEY ? { "X-API-Key": PARSER_API_KEY } : {},
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const message = body?.detail ?? body?.error ?? `Parser returned ${res.status}`;
      return NextResponse.json({ error: message }, { status: res.status });
    }
    return NextResponse.json(body);
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? `Parser unreachable: ${e.message}` : "Parser unreachable" },
      { status: 502 }
    );
  }
}
