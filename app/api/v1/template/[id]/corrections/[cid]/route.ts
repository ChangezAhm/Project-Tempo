import { NextResponse } from "next/server";

// Server-side only. Removes a stored data-model correction on the parser.
const PARSER_URL = process.env.PARSER_SERVICE_URL ?? "http://localhost:8000";
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export async function DELETE(
  _req: Request,
  { params }: { params: Promise<{ id: string; cid: string }> }
) {
  const { id, cid } = await params;
  try {
    const res = await fetch(`${PARSER_URL}/datamodel/${id}/corrections/${cid}`, {
      method: "DELETE",
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
