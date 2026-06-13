import { NextResponse } from "next/server";

// Server-side only. Defaults to the local parser service; override with
// PARSER_SERVICE_URL in the environment for other deployments.
const PARSER_URL = process.env.PARSER_SERVICE_URL ?? "http://localhost:8000";
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export async function POST(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  try {
    const res = await fetch(`${PARSER_URL}/parse/${id}`, {
      method: "POST",
      headers: PARSER_API_KEY ? { "X-API-Key": PARSER_API_KEY } : {},
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      // FastAPI puts error text under `detail`.
      const message =
        body?.detail ?? body?.error ?? `Parser returned ${res.status}`;
      return NextResponse.json({ error: message }, { status: res.status });
    }
    return NextResponse.json(body);
  } catch (e) {
    return NextResponse.json(
      {
        error:
          e instanceof Error
            ? `Parser unreachable: ${e.message}`
            : "Parser unreachable",
      },
      { status: 502 }
    );
  }
}
