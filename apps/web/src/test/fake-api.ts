// A fake observer API on `globalThis.fetch`, typed from the generated OpenAPI paths.
import { vi } from "vitest";
import type { paths } from "../api/schema";
import * as fixtures from "./fixtures";

type Method = "get" | "put" | "post" | "delete" | "patch";
type OkBody<Operation> = Operation extends {
  responses: { 200: { content: { "application/json": infer Body } } };
}
  ? Body
  : never;

type RouteEntry = {
  [Path in keyof paths]: {
    [M in Method]: [OkBody<paths[Path][M]>] extends [never]
      ? never
      : { route: `${Uppercase<M>} ${Path}`; body: OkBody<paths[Path][M]> };
  }[Method];
}[keyof paths];

/** An API operation, written as "METHOD /path/{param}" like the OpenAPI document. */
export type Route = RouteEntry["route"];
export type ResponseBody<R extends Route> = Extract<RouteEntry, { route: R }>["body"];

export type ApiCall = {
  route: Route;
  url: URL;
  headers: Headers;
  body: unknown;
};

export type Reply<R extends Route> = ResponseBody<R> | ((call: ApiCall) => ResponseBody<R>);
export type Replies = { [R in Route]?: Reply<R> };
/** A dropped connection, or an HTTP error status. */
export type Failure = "network" | number;

const defaults: Replies = {
  "GET /api/catalog": fixtures.catalog,
  "GET /api/selection": fixtures.emptySelection,
  "GET /api/monitored": fixtures.emptyMonitored,
  "GET /api/health": fixtures.health,
  "GET /api/coverage": fixtures.emptyCoverage,
  "GET /api/events": fixtures.emptyEvents,
  "POST /api/discovery/refresh": fixtures.discoveryRefresh,
  "PUT /api/selection/{entity_id}": fixtures.emptySelection,
};

const routePattern = (route: string): { method: string; pattern: RegExp } => {
  const [method = "", template = ""] = route.split(" ");
  const source = template.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\{[^}]+\}/g, "[^/]+");
  return { method, pattern: new RegExp(`^${source}$`) };
};

const isRoute = (key: string, replies: Replies): key is Route => key in replies;

/** Installs the fake on `globalThis.fetch`. Undo with `vi.unstubAllGlobals()`. */
export function installFakeApi(overrides: Replies = {}) {
  const replies: Replies = { ...defaults, ...overrides };
  const failures = new Map<Route, Failure>();
  const calls: ApiCall[] = [];
  const unmatched: string[] = [];

  const match = (method: string, pathname: string): Route | undefined =>
    Object.keys(replies)
      .filter((key) => isRoute(key, replies))
      .find((key) => {
        const route = routePattern(key);
        return route.method === method && route.pattern.test(pathname);
      });

  const fakeFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = new Request(
      input instanceof Request ? input : new URL(String(input), window.location.href),
      init,
    );
    const url = new URL(request.url);
    const route = match(request.method, url.pathname);
    if (!route) {
      unmatched.push(`${request.method} ${url.pathname}${url.search}`);
      return new Response(JSON.stringify({ detail: "Not Found" }), {
        status: 404,
      });
    }
    const text = await request.text();
    const call: ApiCall = {
      route,
      url,
      headers: request.headers,
      body: text ? JSON.parse(text) : undefined,
    };
    calls.push(call);
    const failure = failures.get(route);
    if (failure === "network") throw new TypeError("Failed to fetch");
    if (failure !== undefined) {
      return new Response(JSON.stringify({ detail: "Observer error" }), {
        status: failure,
      });
    }
    const reply = replies[route];
    const body = typeof reply === "function" ? reply(call) : reply;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  vi.stubGlobal("fetch", vi.fn(fakeFetch));

  return {
    calls,
    /** Requests that matched no route; a test should end with none. */
    unmatched,
    callsTo: (route: Route): ApiCall[] => calls.filter((call) => call.route === route),
    reply: <R extends Route>(route: R, reply: Reply<R>): void => {
      failures.delete(route);
      Object.assign(replies, { [route]: reply });
    },
    fail: (route: Route, failure: Failure): void => {
      failures.set(route, failure);
    },
  };
}

export type FakeApi = ReturnType<typeof installFakeApi>;
