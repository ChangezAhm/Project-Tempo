import { NextResponse } from "next/server";

// Live populate stage for the add-in pane (polled during a fill).
const PARSER_URL = (process.env.PARSER_SERVICE_URL ?? "http://localhost:8000")
  .replace("//localhost", "//127.0.0.1");
const PARSER_API_KEY = process.env.PARSER_API_KEY ?? "";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  try {
    const res = await fetch(`${PARSER_URL}/populate-progress/${id}`, {
      headers: PARSER_API_KEY ? { "X-API-Key": PARSER_API_KEY } : {},
      cache: "no-store",
    });
    return NextResponse.json(await res.json(), { status: res.status });
  } catch {
    return NextResponse.json({ stage: null }, { status: 200 });
  }
}
