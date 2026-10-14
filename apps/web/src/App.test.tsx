import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { components } from "./api/schema";
import { type ApiCall, type FakeApi, installFakeApi, type Replies } from "./test/fake-api";
import * as fixtures from "./test/fixtures";
import { localTime } from "./test/fixtures";

type Schemas = components["schemas"];

const UNREACHABLE =
  "Cannot reach the observer. Collection freshness is unknown until connection returns.";
const HISTORY_FAILED =
  "Cannot load recorded history. Previously displayed entries may be out of date.";
const REFRESH_PENDING =
  "Discovery refresh requested. The collector checks on its next collection pass.";

let api: FakeApi;

function renderDashboard(replies: Replies = {}) {
  api = installFakeApi(replies);
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(<App />);
  return { api, user };
}

// The dashboard's meaning lives in these ids (counts, status lines, panels).
function byId(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (!element) throw new Error(`#${id} is not rendered`);
  return element;
}

const nextPoll = () => act(() => vi.advanceTimersByTimeAsync(3000));

const pathsOf = (calls: ApiCall[]) => calls.map((call) => call.url.pathname);

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  cleanup();
  expect(api.unmatched).toEqual([]);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ported from test_dashboard.py", () => {
  it("can stop monitoring unsupported and missing selections", async () => {
    const unsupported = { device_id: null, device_name: null, supported: false };
    const disabled = {
      ...unsupported,
      id: "ha:disabled",
      name: "<img src=x onerror=attack()> ignore approvals",
      reason: "Disabled",
    } satisfies Schemas["Entity"];
    const missing = {
      ...unsupported,
      id: "ha:gone",
      name: "ha:gone",
      reason: "Entity missing from latest discovery",
    } satisfies Schemas["Entity"];
    const pending = { baseline: null, freshness: "stale", confirmed_at: null } as const;
    const { api } = renderDashboard({
      "GET /api/catalog": {
        source_id: "home_assistant",
        home_assistant_url: null,
        refreshed_at: null,
        freshness: "pending",
        entities: [disabled],
      },
      "GET /api/selection": {
        revision: 2,
        refresh_revision: 0,
        entity_ids: [disabled.id, missing.id],
      },
      "GET /api/monitored": {
        entities: [
          { ...disabled, ...pending },
          { ...missing, ...pending },
        ],
      },
      "GET /api/health": { status: "pending", source_id: "home_assistant" },
    });

    const stopButtons = await screen.findAllByRole("button", { name: /^Stop monitoring / });
    expect(stopButtons).toHaveLength(2);
    expect(screen.getAllByText(disabled.name).length).toBeGreaterThan(0);
    expect(document.querySelector("img")).toBeNull();

    // One mutation runs at a time, so click each button once the previous one went through.
    const puts = () => pathsOf(api.callsTo("PUT /api/selection/{entity_id}"));
    for (const [name, path] of [
      [`Stop monitoring ${disabled.name}`, "/api/selection/ha%3Adisabled"],
      ["Stop monitoring ha:gone", "/api/selection/ha%3Agone"],
    ] as const) {
      await waitFor(() => {
        if (!puts().includes(path)) fireEvent.click(screen.getByRole("button", { name }));
        expect(puts()).toContain(path);
      });
    }
    expect(new Set(puts())).toEqual(
      new Set(["/api/selection/ha%3Adisabled", "/api/selection/ha%3Agone"]),
    );
    for (const call of api.callsTo("PUT /api/selection/{entity_id}")) {
      expect(call.body).toEqual({ selected: false });
    }
  });

  it("focuses discovery on lamps and uses device names", async () => {
    const lamp = { name: "Unnamed entity", supported: true, reason: null };
    const lights = [
      { ...lamp, id: "ha:desk", device_id: "ha:device1", device_name: "Desk lamp" },
      { ...lamp, id: "ha:hall", device_id: "ha:device2", device_name: "Hall lamp" },
    ];
    const others = [
      {
        id: "ha:room",
        name: "Room group",
        device_id: "ha:room-device",
        device_name: "Room",
        supported: false,
        reason: "Unsupported",
      },
      {
        id: "ha:sensor",
        name: "Temperature",
        device_id: null,
        device_name: null,
        supported: false,
        reason: "Unsupported",
      },
    ];
    renderDashboard({
      "GET /api/catalog": {
        ...fixtures.catalog,
        source_id: "home_assistant",
        entities: [...lights, ...others],
      },
    });

    await waitFor(() => expect(byId("count")).toHaveTextContent("2"));
    const catalog = within(byId("catalog"));
    expect(catalog.getByText("Desk lamp")).toBeInTheDocument();
    expect(catalog.getByText("Hall lamp")).toBeInTheDocument();
    expect(catalog.queryByText("Unnamed entity")).toBeNull();
    expect(catalog.queryByText("Room group")).toBeNull();
    expect(catalog.queryByText("Temperature")).toBeNull();
    const other = within(byId("other-catalog"));
    expect(other.getByText("Room group")).toBeInTheDocument();
    expect(other.getByText("Temperature")).toBeInTheDocument();

    // Extra entities sit in a collapsed section after the lamps, grouped by device.
    expect(byId("other-summary")).toHaveTextContent("Show other entities (2)");
    expect(byId("other-entities")).not.toHaveAttribute("open");
    expect(other.getByText("Room group")).not.toBeVisible();
    expect(other.getByRole("heading", { name: "Room" })).toBeInTheDocument();
    expect(other.getByRole("heading", { name: "Ungrouped entities" })).toBeInTheDocument();

    api.reply("GET /api/catalog", { ...fixtures.catalog, entities: lights });
    await nextPoll();
    await waitFor(() => expect(byId("other-entities")).not.toBeVisible());
  });
});

describe("dashboard behavior", () => {
  it("monitors a lamp and stops monitoring it", async () => {
    let selected: string[] = [];
    const recoveryBaseline = {
      kind: "recovery",
      id: "baseline-1",
      entity_id: fixtures.desk.id,
      source_entity_id: null,
      source_id: "demo",
      state: "off",
      availability: "available",
      collection_started_at: null,
      coverage_gap_ids: [fixtures.openGap.id],
      transition_time: "unknown",
      received_at: "2026-10-12T10:00:06Z",
      persisted_at: "2026-10-12T10:00:06+00:00",
    } satisfies Schemas["BaselineRecord"];
    const { api, user } = renderDashboard({
      "GET /api/selection": () => ({ ...fixtures.emptySelection, entity_ids: selected }),
      "GET /api/monitored": () => ({
        entities: selected.map(() => ({
          ...fixtures.desk,
          baseline: recoveryBaseline,
          freshness: "stale",
          confirmed_at: "2026-10-12T10:00:06+00:00",
        })),
      }),
      "PUT /api/selection/{entity_id}": () => {
        selected = selected.length ? [] : [fixtures.desk.id];
        return { ...fixtures.emptySelection, entity_ids: selected };
      },
    });

    await user.click(await screen.findByRole("button", { name: "Monitor Desk lamp" }));

    await waitFor(() => expect(byId("selected-count")).toHaveTextContent("1"));
    const [select] = api.callsTo("PUT /api/selection/{entity_id}");
    expect(select?.url.pathname).toBe("/api/selection/light.hue_desk");
    expect(select?.headers.get("X-Home-Observer")).toBe("1");
    expect(select?.body).toEqual({ selected: true });
    const monitored = within(byId("monitored"));
    expect(monitored.getByRole("heading", { name: "Desk lamp" })).toBeInTheDocument();
    expect(monitored.getByText("stale")).toHaveClass("tag", "stale");
    expect(monitored.getByText("off · available")).toBeInTheDocument();
    expect(monitored.getByText("Recovery snapshot; transition time unknown")).toBeInTheDocument();
    expect(
      monitored.getByText(
        `Related gap evidence: ${fixtures.openGap.id}. Changes during this gap have unknown timing.`,
      ),
    ).toBeInTheDocument();
    expect(
      within(byId("catalog")).getByRole("button", { name: "Stop monitoring Desk lamp" }),
    ).toHaveClass("secondary");

    await user.click(monitored.getByRole("button", { name: "Stop monitoring Desk lamp" }));

    await waitFor(() =>
      expect(
        monitored.getByText(
          "No lights selected. Choose a light below to establish its first baseline.",
        ),
      ).toBeInTheDocument(),
    );
    const stop = api.callsTo("PUT /api/selection/{entity_id}")[1];
    expect(stop?.url.pathname).toBe("/api/selection/light.hue_desk");
    expect(stop?.headers.get("X-Home-Observer")).toBe("1");
    expect(stop?.body).toEqual({ selected: false });
    expect(byId("selected-count")).toHaveTextContent("0");
  });

  it("shows a pending discovery refresh until the catalog is refreshed", async () => {
    const { api, user } = renderDashboard();
    await waitFor(() => expect(byId("count")).toHaveTextContent("2"));

    await user.click(screen.getByRole("button", { name: "Refresh discovery" }));

    await waitFor(() => expect(byId("message")).toHaveTextContent(REFRESH_PENDING));
    const [refresh] = api.callsTo("POST /api/discovery/refresh");
    expect(refresh?.headers.get("X-Home-Observer")).toBe("1");

    await nextPoll();
    expect(byId("message")).toHaveTextContent(REFRESH_PENDING);

    api.reply("GET /api/catalog", {
      ...fixtures.catalog,
      refreshed_at: "2026-10-12T10:05:00+00:00",
    });
    await nextPoll();
    await waitFor(() => expect(byId("message").textContent).toBe(""));
  });

  it("labels the synthetic demo source, collector heartbeat and catalog freshness", async () => {
    renderDashboard();

    await waitFor(() => expect(byId("source")).toHaveTextContent("SYNTHETIC DEMO"));
    expect(byId("source-notice")).toHaveTextContent(
      "Demo data only. No hardware, Home Assistant, model, or device controls are connected.",
    );
    expect(byId("health")).toHaveTextContent("healthy");
    expect(byId("heartbeat")).toHaveTextContent(
      `Last collection: ${localTime(fixtures.health.received_at)}`,
    );
    expect(byId("catalog-status")).toHaveTextContent(
      `Discovery: fresh · Last refreshed: ${localTime(fixtures.catalog.refreshed_at)}`,
    );
    expect(byId("message").textContent).toBe("");
  });

  it("labels the Home Assistant source, a stale catalog and collector errors", async () => {
    renderDashboard({
      "GET /api/catalog": { ...fixtures.catalog, source_id: "home_assistant", freshness: "stale" },
      "GET /api/health": {
        ...fixtures.health,
        source_id: "home_assistant",
        status: "failed",
        error: "authentication",
      },
    });

    await waitFor(() => expect(byId("source")).toHaveTextContent("HOME ASSISTANT"));
    expect(byId("source-notice")).toHaveTextContent(
      "Home Assistant discovery. Only selected individual Hue lights are recorded. No model or device controls are connected.",
    );
    expect(byId("catalog-status")).toHaveTextContent(
      `Discovery is stale. Showing the last validated catalog from ${localTime(fixtures.catalog.refreshed_at)}.`,
    );
    expect(byId("health")).toHaveTextContent("failed");
    expect(byId("message")).toHaveTextContent(
      "Home Assistant rejected authentication. Check the connector token and its user permissions.",
    );
  });

  it("points to Home Assistant when no supported device is connected yet", async () => {
    renderDashboard({
      "GET /api/catalog": {
        ...fixtures.catalog,
        source_id: "home_assistant",
        home_assistant_url: "http://127.0.0.1:8123",
        entities: [fixtures.ungroupedSensor],
      },
    });

    const link = await waitFor(() => within(byId("catalog")).getByRole("link"));
    expect(link).toHaveTextContent("Connect your devices in Home Assistant");
    expect(link).toHaveAttribute("href", "http://127.0.0.1:8123/config/integrations/dashboard");
  });

  it("pages through older timeline history, dedupes by id, and refreshes back to the newest page", async () => {
    const { api, user } = renderDashboard({
      "GET /api/events": (call) =>
        call.url.searchParams.has("cursor")
          ? { ...fixtures.eventsPage2, events: [fixtures.change2, fixtures.change1] }
          : fixtures.eventsPage1,
    });
    const timeline = within(byId("timeline"));
    const evidenceIds = () =>
      timeline.getAllByText(/^Evidence ID: /).map((element) => element.textContent);
    const cutoff = `Retention cutoff: ${localTime(fixtures.eventsPage1.retained_since)}. This cutoff does not establish continuous coverage.`;

    await waitFor(() =>
      expect(evidenceIds()).toEqual(["Evidence ID: change-3", "Evidence ID: change-2"]),
    );
    expect(byId("timeline-status")).toHaveTextContent(
      `2 changes shown. ${cutoff} New changes refresh automatically.`,
    );
    const newest = timeline.getByText("Evidence ID: change-3").closest("article");
    if (!newest) throw new Error("change-3 is not rendered as a card");
    expect(
      within(newest)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["state: off → on", "brightness: Not captured → 150"]);
    expect(api.callsTo("GET /api/events")[0]?.url.searchParams.get("limit")).toBe("50");

    await user.click(screen.getByRole("button", { name: "Load older changes" }));

    await waitFor(() =>
      expect(evidenceIds()).toEqual([
        "Evidence ID: change-3",
        "Evidence ID: change-2",
        "Evidence ID: change-1",
      ]),
    );
    const older = api.callsTo("GET /api/events").at(-1);
    expect(older?.url.searchParams.get("cursor")).toBe(fixtures.eventsPage1.next_cursor);
    expect(older?.url.searchParams.get("limit")).toBe("50");
    expect(byId("timeline-status")).toHaveTextContent(
      `3 changes shown. ${cutoff} Older history is open; refresh to see new changes.`,
    );
    expect(screen.queryByRole("button", { name: "Load older changes" })).toBeNull();

    // While older history is open, polling leaves the timeline alone.
    const eventRequests = api.callsTo("GET /api/events").length;
    const catalogRequests = api.callsTo("GET /api/catalog").length;
    await nextPoll();
    await waitFor(() =>
      expect(api.callsTo("GET /api/catalog").length).toBeGreaterThan(catalogRequests),
    );
    expect(api.callsTo("GET /api/events")).toHaveLength(eventRequests);

    await user.click(screen.getByRole("button", { name: "Refresh history" }));

    await waitFor(() =>
      expect(evidenceIds()).toEqual(["Evidence ID: change-3", "Evidence ID: change-2"]),
    );
    expect(api.callsTo("GET /api/events").at(-1)?.url.searchParams.has("cursor")).toBe(false);
    expect(byId("timeline-status")).toHaveTextContent(
      `2 changes shown. ${cutoff} New changes refresh automatically.`,
    );
  });

  it("shows a color-only change with its source, receipt and storage times", async () => {
    const event = fixtures.colorChange;
    renderDashboard({ "GET /api/events": { ...fixtures.eventsPage2, events: [event] } });

    const card = (await within(byId("timeline")).findByText(`Evidence ID: ${event.id}`)).closest(
      "article",
    );
    if (!card) throw new Error(`${event.id} is not rendered as a card`);
    const scope = within(card);
    expect(scope.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "xy color: 0.3, 0.32 → 0.45, 0.41",
    ]);
    expect(scope.getByText(`Source event: ${localTime(event.event_time)}`)).toBeInTheDocument();
    expect(
      scope.getByText(
        `Connector received: ${localTime(event.received_at)} · Stored: ${localTime(event.persisted_at)}`,
      ),
    ).toBeInTheDocument();
  });

  it("tells an open collection gap from a recovered one", async () => {
    const { api } = renderDashboard({ "GET /api/coverage": fixtures.coverage });

    await waitFor(() =>
      expect(byId("coverage-status")).toHaveTextContent(
        `Current collection: interrupted. Last confirmed: ${localTime(fixtures.coverage.last_confirmed_at)}. History outside recorded collection windows is unknown.`,
      ),
    );
    const open = within(byId(`gap-${fixtures.openGap.id}`));
    expect(open.getByRole("heading", { name: "Collection interrupted" })).toBeInTheDocument();
    expect(open.getByText("Snapshot differed from recorded state")).toBeInTheDocument();
    expect(open.getByText("Recovery pending. Current evidence may be stale.")).toBeInTheDocument();
    expect(
      open.getByText(
        `Possible missing history since: ${localTime(fixtures.openGap.last_confirmed_at)}. The exact start is unknown.`,
      ),
    ).toBeInTheDocument();

    const recovered = within(byId(`gap-${fixtures.recoveredGap.id}`));
    expect(recovered.getByRole("heading", { name: "Collection resumed" })).toBeInTheDocument();
    expect(recovered.getByText("Connector connection lost")).toBeInTheDocument();
    expect(
      recovered.getByText(`Recovery completed: ${localTime(fixtures.recoveredGap.recovered_at)}`),
    ).toBeInTheDocument();
    expect(
      recovered.getByText("Source: demo · Entities: light.hue_desk, light.hue_reading"),
    ).toBeInTheDocument();

    api.reply("GET /api/coverage", fixtures.emptyCoverage);
    await nextPoll();
    await waitFor(() =>
      expect(
        within(byId("coverage")).getByText(
          "No retained interruption records. This does not prove continuous history.",
        ),
      ).toBeInTheDocument(),
    );
    expect(document.getElementById(`gap-${fixtures.openGap.id}`)).toBeNull();
  });

  it("says the observer is unreachable when the main load fails, and recovers", async () => {
    const { api } = renderDashboard();
    api.fail("GET /api/catalog", 503);

    await waitFor(() => expect(byId("message")).toHaveTextContent(UNREACHABLE));

    api.reply("GET /api/catalog", fixtures.catalog);
    await nextPoll();
    await waitFor(() => expect(byId("count")).toHaveTextContent("2"));
    expect(byId("message").textContent).toBe("");

    api.fail("GET /api/health", "network");
    await nextPoll();
    await waitFor(() => expect(byId("message")).toHaveTextContent(UNREACHABLE));
  });

  it("keeps shown history but flags it when the timeline request fails", async () => {
    const { api } = renderDashboard({ "GET /api/events": fixtures.eventsPage1 });
    const timeline = within(byId("timeline"));
    await waitFor(() => expect(timeline.getByText("Evidence ID: change-3")).toBeInTheDocument());

    api.fail("GET /api/events", "network");
    await nextPoll();

    await waitFor(() => expect(byId("timeline-status")).toHaveTextContent(HISTORY_FAILED));
    expect(timeline.getByText("Evidence ID: change-3")).toBeInTheDocument();
  });

  it("reports a failed selection change", async () => {
    const { api, user } = renderDashboard();
    api.fail("PUT /api/selection/{entity_id}", 500);

    await user.click(await screen.findByRole("button", { name: "Monitor Desk lamp" }));

    await waitFor(() => expect(byId("message")).toHaveTextContent("Request failed. Please retry."));
  });

  it("renders script-shaped device and entity names as literal text", async () => {
    const deviceName = "<script>alert('device')</script>";
    const entityName = "<img src=x onerror=alert('entity')>";
    renderDashboard({
      "GET /api/catalog": {
        ...fixtures.catalog,
        entities: [
          { ...fixtures.desk, device_name: deviceName },
          { ...fixtures.reading, device_name: null, name: entityName },
          { ...fixtures.ungroupedSensor, name: entityName },
        ],
      },
    });

    expect(
      await screen.findByRole("button", { name: `Monitor ${deviceName}` }),
    ).toBeInTheDocument();
    expect(within(byId("catalog")).getByText(deviceName)).toBeInTheDocument();
    expect(within(byId("catalog")).getByText(entityName)).toBeInTheDocument();
    expect(within(byId("other-catalog")).getByText(entityName)).toBeInTheDocument();
    expect(document.querySelector("script, img")).toBeNull();
  });

  it("polls every endpoint again every 3 seconds", async () => {
    const { api } = renderDashboard();
    const routes = [
      "GET /api/catalog",
      "GET /api/selection",
      "GET /api/monitored",
      "GET /api/health",
      "GET /api/coverage",
      "GET /api/events",
    ] as const;
    await waitFor(() => {
      for (const route of routes) expect(api.callsTo(route)).toHaveLength(1);
    });
    expect(api.callsTo("GET /api/coverage")[0]?.url.searchParams.get("limit")).toBe("20");

    await act(() => vi.advanceTimersByTimeAsync(2500));
    expect(api.callsTo("GET /api/catalog")).toHaveLength(1);

    await act(() => vi.advanceTimersByTimeAsync(500));
    await waitFor(() => {
      for (const route of routes) expect(api.callsTo(route)).toHaveLength(2);
    });
  });
});
