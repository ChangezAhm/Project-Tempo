// Server-only fetch for LONG-running parser calls (understand / populate run for
// minutes). Node's built-in fetch aborts these at its ~300s header timeout even
// though the parser finishes. We use undici's OWN fetch + Agent (same package,
// so the dispatcher is compatible — passing an undici Agent to Node's built-in
// fetch fails instantly) with the timeouts disabled.
import { Agent, fetch as undiciFetch } from "undici";

const longLived = new Agent({ headersTimeout: 0, bodyTimeout: 0 });

export async function parserFetch(
  url: string,
  init: { method?: string; headers?: Record<string, string>; body?: Uint8Array | string } = {}
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<{ ok: boolean; status: number; json: () => Promise<any> }> {
  const res = await undiciFetch(url, { ...init, dispatcher: longLived });
  return { ok: res.ok, status: res.status, json: () => res.json() };
}
