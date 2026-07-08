import { type NextRequest } from "next/server";
import { updateSession } from "@/utils/supabase/middleware";

// Next.js 16 renamed Middleware to Proxy. This runs before every matched
// request and keeps the Supabase session fresh.
export async function proxy(request: NextRequest) {
  return await updateSession(request);
}

export const config = {
  matcher: [
    // Run on everything except API routes (no auth yet — session refresh is
    // pointless overhead there), static assets and image files.
    "/((?!api/|_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
