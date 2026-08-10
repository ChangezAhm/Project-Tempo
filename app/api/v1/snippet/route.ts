import { NextResponse } from "next/server";
import sharp from "sharp";
import { createAdminClient } from "@/utils/supabase/admin";

// Sheet-screenshot proxy — the fix for slow template pages.
//
// The old path signed a FRESH Supabase URL on every page load: the token in
// the query string changed each time, so the browser could never cache, and
// full-sheet renders (1–1.6 MB PNGs) re-downloaded on every visit. This route
// serves the same images at a STABLE URL, downscaled to what the UI actually
// renders, with immutable cache headers — first load is ~30–80 KB webp, every
// later load is free (browser cache), and a small in-memory LRU makes even
// cold hits cheap.
const BUCKET = "template-snippets";
const PATH_RE = /^[0-9a-f-]{36}\/[^/\\]+\.png$/i;
const WIDTHS = new Set([640, 1400, 2000]);

const cache = new Map<string, Uint8Array>(); // key: `${path}|${w}`
const MAX_ENTRIES = 300;

export async function GET(req: Request) {
  const url = new URL(req.url);
  const path = url.searchParams.get("path") ?? "";
  const w = Number(url.searchParams.get("w") ?? 1400);
  if (!PATH_RE.test(path) || !WIDTHS.has(w)) {
    return NextResponse.json({ error: "Bad snippet request" }, { status: 400 });
  }

  const key = `${path}|${w}`;
  let body = cache.get(key);
  if (!body) {
    const supabase = createAdminClient();
    const { data, error } = await supabase.storage.from(BUCKET).download(path);
    if (error || !data) {
      return NextResponse.json({ error: "Not found" }, { status: 404 });
    }
    const raw = Buffer.from(await data.arrayBuffer());
    body = new Uint8Array(
      await sharp(raw).resize({ width: w, withoutEnlargement: true }).webp({ quality: 78 }).toBuffer()
    );
    if (cache.size >= MAX_ENTRIES) {
      const first = cache.keys().next().value;
      if (first) cache.delete(first);
    }
    cache.set(key, body);
  }

  return new NextResponse(body.slice().buffer as ArrayBuffer, {
    headers: {
      "Content-Type": "image/webp",
      // Snippets are content-stable per version (re-onboarding overwrites the
      // object, but a week of staleness on a screenshot is acceptable UX).
      "Cache-Control": "public, max-age=604800, immutable",
    },
  });
}
