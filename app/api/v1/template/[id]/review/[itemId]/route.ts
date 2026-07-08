import { NextResponse } from "next/server";

// Server-side only. Updates a single review item (answer / dismiss / reopen)
// on the parser service.
const PARSER_URL = process.env.PARSER_SERVICE_URL ?? "http://localhost:8000";
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export async function PATCH(
  req: Request,
  { params }: { params: Promise<{ id: string; itemId: string }> }
) {
  const { id, itemId } = await params;
  const patch = await req.json().catch(() => null);
  if (!patch || typeof patch !== "object") {
    return NextResponse.json({ error: "A JSON body is required" }, { status: 400 });
  }
  try {
    const res = await fetch(`${PARSER_URL}/review/${id}/items/${itemId}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        ...(PARSER_API_KEY ? { "X-API-Key": PARSER_API_KEY } : {}),
      },
      body: JSON.stringify(patch),
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
