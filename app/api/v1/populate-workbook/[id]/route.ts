import { NextResponse } from "next/server";
import { Agent } from "undici";

// Excel add-in populate proxy. The task pane is HTTPS and same-origin with the
// site; the parser is plain HTTP on localhost — proxying here avoids both
// mixed-content blocking and CORS. A populate run can hold the connection for
// many minutes, so the default undici header timeout (5 min) is disabled.
const PARSER_URL = process.env.PARSER_SERVICE_URL ?? "http://localhost:8000";
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

const longRun = new Agent({ headersTimeout: 0, bodyTimeout: 0 });

export async function POST(
  req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const body = await req.text();
  try {
    const res = await fetch(`${PARSER_URL}/populate-workbook/${id}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(PARSER_API_KEY ? { "X-API-Key": PARSER_API_KEY } : {}),
      },
      body,
      cache: "no-store",
      // @ts-expect-error undici dispatcher is a Node-fetch extension
      dispatcher: longRun,
    });
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (e) {
    const message = e instanceof Error ? e.message : "Parser unreachable";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
