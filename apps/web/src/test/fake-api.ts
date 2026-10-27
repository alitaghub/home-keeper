// A fake keeper API on `globalThis.fetch`, typed from the generated OpenAPI paths.
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
export type Failure = "network" | number | { status: number; body: unknown };

const defaults: Replies = {
  "GET /api/catalog": fixtures.catalog,
  "GET /api/selection": fixtures.emptySelection,
  "GET /api/monitored": fixtures.emptyMonitored,
  "GET /api/control": fixtures.controlOff,
  "GET /api/health": fixtures.health,
  "GET /api/coverage": fixtures.emptyCoverage,
  "GET /api/events": fixtures.emptyEvents,
  "GET /api/commands": fixtures.emptyCommands,
  "POST /api/discovery/refresh": fixtures.discoveryRefresh,
  "PUT /api/selection/{entity_id}": fixtures.emptySelection,
};

const routePattern = (route: string): { method: string; pattern: RegExp } => {
  const [method = "", template = ""] = route.split(" ");
  const source = template.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\{[^}]+\}/g, "[^/]+");
  return { method, pattern: new RegExp(`^${source}$`) };
};

/** Installs the fake on `globalThis.fetch`. Undo with `vi.unstubAllGlobals()`. */
export function installFakeApi(overrides: Replies = {}) {
  // Object.entries widens keys to string; every key of a Replies object is a Route.
  const replies = new Map(Object.entries({ ...defaults, ...overrides }) as [Route, Reply<Route>][]);
  const failures = new Map<Route, Failure>();
  const holds = new Map<Route, Promise<void>>();
  const calls: ApiCall[] = [];
  const unmatched: string[] = [];

  const match = (method: string, pathname: string): Route | undefined =>
    [...replies.keys()].find((key) => {
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
    // A held route answers only after release(); the call is already recorded.
    await holds.get(route);
    const failure = failures.get(route);
    if (failure === "network") throw new TypeError("Failed to fetch");
    if (failure !== undefined) {
      const { status, body } =
        typeof failure === "number"
          ? { status: failure, body: { detail: "Keeper error" } }
          : failure;
      return new Response(JSON.stringify(body), { status });
    }
    const reply = replies.get(route);
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
      replies.set(route, reply);
    },
    fail: (route: Route, failure: Failure): void => {
      failures.set(route, failure);
    },
    /** Keeps requests to `route` pending until the returned function runs. */
    hold: (route: Route): (() => void) => {
      let release = () => {};
      holds.set(
        route,
        new Promise<void>((resolve) => {
          release = resolve;
        }),
      );
      return () => {
        holds.delete(route);
        release();
      };
    },
  };
}

export type FakeApi = ReturnType<typeof installFakeApi>;
