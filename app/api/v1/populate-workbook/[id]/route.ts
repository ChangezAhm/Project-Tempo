import { NextResponse } from "next/server";
import { request } from "undici";

// Excel add-in populate proxy. The task pane is HTTPS and same-origin with the
// site; the parser is plain HTTP on localhost — proxying here avoids both
// mixed-content blocking and CORS. Uses undici.request directly (NOT the
// Next-patched global fetch, which rejects a foreign undici Agent) with
// timeouts disabled: a populate run can hold the connection for many minutes.
// localhost is pinned to IPv4 — uvicorn binds 127.0.0.1, not ::1.
const PARSER_URL = (process.env.PARSER_SERVICE_URL ?? "http://localhost:8000")
  .replace("//localhost", "//127.0.0.1");
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export async function POST(
  req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.text();
  try {
    const res = await request(`${PARSER_URL}/populate-workbook/${id}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(PARSER_API_KEY ? { "x-api-key": PARSER_API_KEY } : {}),
      },
      body,
      headersTimeout: 0,
      bodyTimeout: 0,
    });
    const text = await res.body.text();
    return new NextResponse(text, {
      status: res.statusCode,
      headers: { "Content-Type": "application/json" },
    });
  } catch (e) {
    const message = e instanceof Error ? e.message : "Parser unreachable";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
