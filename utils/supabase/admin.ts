import { createClient, type SupabaseClient } from "@supabase/supabase-js";

// Server-only Supabase client for the /api/v1 template routes. Never import
// this from client components — the service-role key must not reach browsers.
//
// FALLBACK: when SUPABASE_SERVICE_ROLE_KEY isn't set yet we fall back to the
// publishable (anon) key so the app keeps working. This fallback exists ONLY
// for the window before the env var is configured and
// supabase/migrations/0007_lock_rls.sql is applied; once 0007 runs, the anon
// key loses table/storage access and the service-role key is required.
export function createAdminClient(): SupabaseClient {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL!;
  const key =
    process.env.SUPABASE_SERVICE_ROLE_KEY ??
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!;
  return createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}
