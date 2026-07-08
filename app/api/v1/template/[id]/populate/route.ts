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
  const form = await req.formData().catch(() => null);
  const file = form?.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ error: "A source file is required" }, { status: 400 });
  }
  const asOf = form?.get("as_of_date");
  const bytes = new Uint8Array(await file.arrayBuffer());

  const qs = new URLSearchParams({ filename: file.name || "source.xlsx" });
  if (typeof asOf === "string" && asOf) qs.set("as_of_date", asOf);
  try {
    const res = await parserFetch(`${PARSER_URL}/populate/${id}?${qs.toString()}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        ...(PARSER_API_KEY ? { "X-API-Key": PARSER_API_KEY } : {}),
      },
      body: bytes,
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
