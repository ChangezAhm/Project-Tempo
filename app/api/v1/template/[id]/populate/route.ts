import { NextResponse } from "next/server";
import { parserFetch } from "@/lib/parserFetch";

// Server-side only. Runs population on the parser: LLM maps the source workbook
// to this template's inputs, fills the cells, returns a download URL + report.
// Long-running (LLM matching) — no platform timeout in local dev.
const PARSER_URL = process.env.PARSER_SERVICE_URL ?? "http://localhost:8000";
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export const maxDuration = 800;

export async function POST(
  req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.json().catch(() => ({}));
  const sourceId = body?.source_id;
  if (!sourceId) {
    return NextResponse.json({ error: "source_id is required" }, { status: 400 });
  }
  const qs = new URLSearchParams({ source_id: sourceId });
  if (body?.as_of_date) qs.set("as_of_date", body.as_of_date);
  try {
    const res = await parserFetch(`${PARSER_URL}/populate/${id}?${qs.toString()}`, {
      method: "POST",
      headers: PARSER_API_KEY ? { "X-API-Key": PARSER_API_KEY } : {},
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) {
      return NextResponse.json(
        { error: out?.detail ?? out?.error ?? `Parser returned ${res.status}` },
        { status: res.status }
      );
    }
    return NextResponse.json(out);
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? `Parser unreachable: ${e.message}` : "Parser unreachable" },
      { status: 502 }
    );
  }
}
