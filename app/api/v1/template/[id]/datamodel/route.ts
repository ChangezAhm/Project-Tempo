import { NextResponse } from "next/server";

// Server-side only. Re-derives the template's data model on the parser
// (deterministic pass + stored corrections). Takes a few seconds.
const PARSER_URL = process.env.PARSER_SERVICE_URL ?? "http://localhost:8000";
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export async function POST(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  try {
    const res = await fetch(`${PARSER_URL}/datamodel/${id}`, {
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
