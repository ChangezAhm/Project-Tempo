import { NextResponse } from "next/server";

// Server-side only. Adds a persistent data-model correction (match + patch)
// on the parser service. Corrections re-apply on every re-derive.
const PARSER_URL = process.env.PARSER_SERVICE_URL ?? "http://localhost:8000";
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export async function POST(
  req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const correction = await req.json().catch(() => null);
  if (!correction || typeof correction !== "object") {
    return NextResponse.json({ error: "A JSON body is required" }, { status: 400 });
  }
  try {
    const res = await fetch(`${PARSER_URL}/datamodel/${id}/corrections`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(PARSER_API_KEY ? { "X-API-Key": PARSER_API_KEY } : {}),
      },
      body: JSON.stringify(correction),
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
