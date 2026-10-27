import createClient from "openapi-fetch";
import type { Readable } from "openapi-typescript-helpers";
import type { components, paths } from "./schema";

// Response shapes as openapi-fetch returns them (its `Readable` widens tuples to arrays).
export type Schemas = {
  [Name in keyof components["schemas"]]: Readable<components["schemas"][Name]>;
};

// Resolve `fetch` at call time so tests can replace `globalThis.fetch`.
// Node's `Request` (used under jsdom) rejects relative URLs, so requests are
// built against the page origin, which is the same as a relative URL in a browser.
export const api = createClient<paths>({
  baseUrl: globalThis.location.origin,
  fetch: (request) => globalThis.fetch(request),
});

// The keeper rejects browser mutations without this header.
export const mutationHeaders = { "X-Home-Keeper": "1" } as const;

export async function unwrap<T>(pending: Promise<{ data?: T; response: Response }>): Promise<T> {
  const { data, response } = await pending;
  if (!response.ok || data === undefined) throw new Error("Request failed. Please retry.");
  return data;
}

export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
