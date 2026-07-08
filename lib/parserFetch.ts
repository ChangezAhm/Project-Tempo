// Server-only fetch for LONG-running parser calls (understand / populate run for
// minutes). Node's built-in fetch aborts these at its ~300s header timeout even
// though the parser finishes. We use undici's OWN fetch + Agent (same package,
// so the dispatcher is compatible — passing an undici Agent to Node's built-in
// fetch fails instantly) with the timeouts disabled.
import { Agent, fetch as undiciFetch } from "undici";

const longLived = new Agent({ headersTimeout: 0, bodyTimeout: 0 });

// A parser restart leaves DEAD sockets in the keep-alive pool; the next request
// through them dies instantly with 'TypeError: fetch failed'. Retry exactly once,
// and ONLY when the failure was immediate (a stale pooled socket fails in
// milliseconds) — a connection that dropped minutes into a populate/understand
// run must NOT be retried, or we'd silently re-bill a long LLM run.
const RETRY_WINDOW_MS = 2000;

export async function parserFetch(
  url: string,
  init: { method?: string; headers?: Record<string, string>; body?: Uint8Array | string } = {}
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Promise<{ ok: boolean; status: number; json: () => Promise<any> }> {
  const started = Date.now();
  try {
    const res = await undiciFetch(url, { ...init, dispatcher: longLived });
    return { ok: res.ok, status: res.status, json: () => res.json() };
  } catch (e) {
    if (Date.now() - started > RETRY_WINDOW_MS) throw e;
    const res = await undiciFetch(url, { ...init, dispatcher: longLived });
    return { ok: res.ok, status: res.status, json: () => res.json() };
  }
}
