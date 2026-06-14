// Server-only fetch for LONG-running parser calls (understand / populate run for
// minutes). Node's default undici header timeout (~300s) aborts these mid-run
// even though the parser finishes — so use a dispatcher with timeouts disabled.
import { Agent } from "undici";

const longLived = new Agent({ headersTimeout: 0, bodyTimeout: 0 });

export function parserFetch(url: string, init: RequestInit = {}): Promise<Response> {
  // Node's fetch accepts an undici `dispatcher`; it isn't in the DOM RequestInit type.
  return fetch(url, { ...init, dispatcher: longLived } as RequestInit & { dispatcher: unknown });
}
