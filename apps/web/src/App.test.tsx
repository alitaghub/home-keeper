import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { components } from "./api/schema";
import { createFakeAegra, type FakeAegra, installFakeAegra } from "./test/fake-aegra";
import {
  type ApiCall,
  type FakeApi,
  installFakeApi,
  type Replies,
  type ResponseBody,
} from "./test/fake-api";
import * as fixtures from "./test/fixtures";
import { localTime } from "./test/fixtures";

type Schemas = components["schemas"];
type Reading = NonNullable<ResponseBody<"GET /api/monitored">["entities"][number]["state"]>;

const UNREACHABLE = "Cannot reach Home Keeper. States are unknown until the connection returns.";
const HISTORY_FAILED =
  "Cannot load recorded history. Previously displayed entries may be out of date.";
// The banner every tab shows while the reader or Home Assistant cannot be reached.
const CANT_REACH_HA = /^Can't reach Home Assistant/;
const RECORDER_LINE =
  "History comes from Home Assistant. How far back it goes depends on its recorder settings.";
const RECORDER_DOC =
  "https://github.com/alitaghub/home-keeper/blob/main/docs/home-assistant.md#history-retention";
// Text from the recorded copy History no longer shows.
const COPY_EVIDENCE = /Evidence ID|Retained since|Retention cutoff|received|Stored/i;

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

/** Opens the dashboard on a tab, as a link to `#setup` or `#history` does. */
const startOn = (tab: "history" | "setup") => window.history.replaceState(null, "", `#${tab}`);

const openTab = (name: "Home" | "History" | "Setup") =>
  fireEvent.click(screen.getByRole("tab", { name }));

const pathsOf = (calls: ApiCall[]) => calls.map((call) => call.url.pathname);

/** The History rows, newest first. */
const changeRows = () => Array.from(byId("timeline").querySelectorAll("article"));

/** The Home Assistant time each History row shows, newest first. */
const shownTimes = () =>
  changeRows().map((row) => row.querySelector("time")?.getAttribute("datetime"));

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
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
      supported: true,
      reason: null,
    } satisfies Schemas["Entity"];
    const { api } = renderDashboard({
      "GET /api/catalog": {
        source_id: "home_assistant",
        home_assistant_url: null,
        entities: [disabled],
      },
      "GET /api/selection": { entity_ids: [disabled.id, missing.id] },
      "GET /api/monitored": {
        entities: [
          { ...disabled, source_entity_id: null, state: null, missing: false },
          fixtures.missingDevice(missing),
        ],
      },
      "GET /api/health": { ...fixtures.health, source_id: "home_assistant" },
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
    startOn("setup");
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
    startOn("setup");
    let selected: string[] = [];
    const { api, user } = renderDashboard({
      "GET /api/selection": () => ({ ...fixtures.emptySelection, entity_ids: selected }),
      "GET /api/monitored": () => ({
        entities: selected.map(() => fixtures.monitoredLight(fixtures.desk, "off")),
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
    expect(select?.headers.get("X-Home-Keeper")).toBe("1");
    expect(select?.body).toEqual({ selected: true });
    expect(
      within(byId("catalog")).getByRole("button", { name: "Stop monitoring Desk lamp" }),
    ).toBeVisible();
    expect(within(byId("catalog")).queryByRole("button", { name: "Monitor Desk lamp" })).toBeNull();

    openTab("Home");
    const monitored = within(byId("monitored"));
    expect(monitored.getByRole("heading", { name: "Desk lamp" })).toBeInTheDocument();
    expect(monitored.getByText("Off")).toBeInTheDocument();
    // Home shows what Home Assistant reports now, not a recorded evidence trail.
    expect(byId("monitored")).not.toHaveTextContent("Evidence ID");

    await user.click(monitored.getByRole("button", { name: "Stop monitoring Desk lamp" }));

    await waitFor(() =>
      expect(
        monitored.getByText(
          "No lights monitored yet. Choose the lights Home Keeper should look after.",
        ),
      ).toBeInTheDocument(),
    );
    const stop = api.callsTo("PUT /api/selection/{entity_id}")[1];
    expect(stop?.url.pathname).toBe("/api/selection/light.hue_desk");
    expect(stop?.headers.get("X-Home-Keeper")).toBe("1");
    expect(stop?.body).toEqual({ selected: false });
    expect(byId("selected-count")).toHaveTextContent("0");
  });

  it("labels the synthetic demo source and shows no Home Assistant warning while it answers", async () => {
    renderDashboard();

    await waitFor(() => expect(byId("source")).toHaveTextContent("SYNTHETIC DEMO"));
    expect(byId("source-notice")).toHaveTextContent(
      "Demo data only. No hardware or Home Assistant is connected.",
    );
    expect(screen.queryByText(CANT_REACH_HA)).toBeNull();
    expect(byId("message").textContent).toBe("");
  });

  it("labels the Home Assistant source", async () => {
    renderDashboard({
      "GET /api/catalog": { ...fixtures.catalog, source_id: "home_assistant" },
      "GET /api/health": { ...fixtures.health, source_id: "home_assistant" },
    });

    await waitFor(() => expect(byId("source")).toHaveTextContent("HOME ASSISTANT"));
    expect(byId("source-notice")).toHaveTextContent("Home Assistant discovery.");
    expect(screen.queryByText(CANT_REACH_HA)).toBeNull();
  });

  it("points to Home Assistant when no supported device is connected yet", async () => {
    startOn("setup");
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
    expect(byId("catalog")).toHaveTextContent(
      ". They appear here within seconds. Home Keeper monitors single lights, plugs and switches, not groups or rooms.",
    );
    expect(byId("count").parentElement).toHaveTextContent(
      "Single lights, plugs and switches, without groups or rooms",
    );
  });

  it("pages through older timeline history, dedupes by id, and refreshes back to the newest page", async () => {
    startOn("history");
    const { api, user } = renderDashboard({
      "GET /api/events": (call) =>
        call.url.searchParams.has("cursor")
          ? { ...fixtures.eventsPage2, events: [fixtures.change2, fixtures.change1] }
          : fixtures.eventsPage1,
    });
    const { change1, change2, change3 } = fixtures;

    await waitFor(() => expect(shownTimes()).toEqual([change3.time, change2.time]));
    const [newest] = changeRows();
    if (!newest) throw new Error("change-3 is not rendered as a row");
    expect(newest).toHaveTextContent("turned on at 59%");
    expect(
      within(newest)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["state: off → on", "brightness: Not captured → 150"]);
    expect(api.callsTo("GET /api/events")[0]?.url.searchParams.get("limit")).toBe("50");

    await user.click(screen.getByRole("button", { name: "Load older changes" }));

    await waitFor(() => expect(shownTimes()).toEqual([change3.time, change2.time, change1.time]));
    const older = api.callsTo("GET /api/events").at(-1);
    expect(older?.url.searchParams.get("cursor")).toBe(fixtures.eventsPage1.next_cursor);
    expect(older?.url.searchParams.get("limit")).toBe("50");
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

    await waitFor(() => expect(shownTimes()).toEqual([change3.time, change2.time]));
    expect(api.callsTo("GET /api/events").at(-1)?.url.searchParams.has("cursor")).toBe(false);
  });

  it("drops a device's changes from History when monitoring stops, even with older pages open", async () => {
    startOn("history");
    const { change1, change3, desk, plugChange, smartPlug } = fixtures;
    let selected = [desk.id, smartPlug.id];
    const { user } = renderDashboard({
      "GET /api/catalog": { ...fixtures.catalog, entities: [desk, smartPlug] },
      "GET /api/selection": () => ({ entity_ids: selected }),
      "GET /api/monitored": () => ({
        entities: [
          fixtures.monitoredLight(desk, "on"),
          ...(selected.includes(smartPlug.id) ? [fixtures.monitoredSwitch(smartPlug, "on")] : []),
        ],
      }),
      "PUT /api/selection/{entity_id}": () => {
        selected = [desk.id];
        return { entity_ids: selected };
      },
      // The API leaves out the changes of a device no longer monitored.
      "GET /api/events": (call) => {
        const events = call.url.searchParams.has("cursor")
          ? [change1]
          : [plugChange, change3].filter((event) => selected.includes(event.entity_id));
        return {
          events,
          next_cursor: call.url.searchParams.has("cursor") ? null : "opaque-cursor",
        };
      },
    });

    await waitFor(() => expect(shownTimes()).toEqual([plugChange.time, change3.time]));
    await user.click(screen.getByRole("button", { name: "Load older changes" }));
    await waitFor(() =>
      expect(shownTimes()).toEqual([plugChange.time, change3.time, change1.time]),
    );

    openTab("Home");
    await user.click(screen.getByRole("button", { name: "Stop monitoring Smart plug" }));
    openTab("History");

    await waitFor(() => expect(shownTimes()).toEqual([change3.time]));
  });

  it("keeps a removed device out of History when an older page it requested arrives late", async () => {
    startOn("history");
    const { api, user } = renderDashboard({
      "GET /api/selection": { entity_ids: [fixtures.desk.id] },
      "GET /api/monitored": { entities: [fixtures.monitoredLight(fixtures.desk, "on")] },
      "GET /api/events": fixtures.eventsPage1,
    });
    await waitFor(() => expect(changeRows()).toHaveLength(2));
    const release = api.hold("GET /api/events", (call) => call.url.searchParams.has("cursor"));
    await user.click(screen.getByRole("button", { name: "Load older changes" }));
    await waitFor(() =>
      expect(api.callsTo("GET /api/events").at(-1)?.url.searchParams.has("cursor")).toBe(true),
    );

    api.reply("GET /api/selection", fixtures.emptySelection);
    api.reply("GET /api/monitored", fixtures.emptyMonitored);
    api.reply("GET /api/events", (call) =>
      call.url.searchParams.has("cursor") ? fixtures.eventsPage2 : fixtures.emptyEvents,
    );
    openTab("Home");
    await user.click(screen.getByRole("button", { name: "Stop monitoring Desk lamp" }));
    await waitFor(() => expect(byId("selected-count")).toHaveTextContent("0"));
    // The older page was asked for under the old selection.
    release();
    await act(() => vi.advanceTimersByTimeAsync(100));

    openTab("History");
    expect(changeRows()).toEqual([]);
    await nextPoll();
    expect(changeRows()).toEqual([]);
  });

  it("shows a change at Home Assistant's time with its entity, and no evidence or receipt times", async () => {
    startOn("history");
    const event = fixtures.colorChange;
    renderDashboard({ "GET /api/events": { ...fixtures.eventsPage2, events: [event] } });

    const [card] = await within(byId("timeline")).findAllByRole("article");
    if (!card) throw new Error(`${event.id} is not rendered as a row`);
    expect(
      within(card)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["xy color: 0.3, 0.32 → 0.45, 0.41"]);
    expect(card.querySelector("time")).toHaveAttribute("datetime", event.time);
    expect(card).toHaveTextContent(event.source_entity_id);
    expect(card).not.toHaveTextContent(COPY_EVIDENCE);
    expect(card).not.toHaveTextContent(event.id);
  });

  it("says history comes from Home Assistant and links to its recorder settings", async () => {
    startOn("history");
    renderDashboard({ "GET /api/events": fixtures.eventsPage1 });

    const link = await screen.findByRole("link", { name: "recorder settings" });
    expect(link).toHaveAttribute("href", RECORDER_DOC);
    expect(link.parentElement?.textContent).toBe(RECORDER_LINE);
    await waitFor(() => expect(changeRows()).toHaveLength(2));
    expect(byId("changes-heading").closest("section")).not.toHaveTextContent(COPY_EVIDENCE);
  });

  it("has no collection gaps section, since Home Assistant keeps the history", async () => {
    startOn("history");
    renderDashboard({ "GET /api/events": fixtures.eventsPage1 });

    await waitFor(() => expect(changeRows()).toHaveLength(2));
    expect(screen.queryByRole("heading", { name: "Collection gaps" })).toBeNull();
    expect(document.body).not.toHaveTextContent(/Collection gaps|Current collection/);
  });

  it("says Home Keeper is unreachable when the main load fails, and recovers", async () => {
    const { api } = renderDashboard();
    // A 503 from the catalog means Home Assistant is down; a dropped request means Home Keeper is.
    api.fail("GET /api/monitored", "network");

    await waitFor(() => expect(byId("message")).toHaveTextContent(UNREACHABLE));

    api.reply("GET /api/monitored", fixtures.emptyMonitored);
    await nextPoll();
    await waitFor(() => expect(byId("count")).toHaveTextContent("2"));
    expect(byId("message").textContent).toBe("");

    api.fail("GET /api/health", "network");
    await nextPoll();
    await waitFor(() => expect(byId("message")).toHaveTextContent(UNREACHABLE));
  });

  it("keeps shown history but flags it when the timeline request fails", async () => {
    renderDashboard({ "GET /api/events": fixtures.eventsPage1 });
    await waitFor(() =>
      expect(shownTimes()).toEqual([fixtures.change3.time, fixtures.change2.time]),
    );

    api.fail("GET /api/events", "network");
    await nextPoll();

    await waitFor(() => expect(document.body).toHaveTextContent(HISTORY_FAILED));
    expect(shownTimes()).toEqual([fixtures.change3.time, fixtures.change2.time]);
  });

  it("reports a failed selection change", async () => {
    startOn("setup");
    const { api, user } = renderDashboard();
    api.fail("PUT /api/selection/{entity_id}", 500);

    await user.click(await screen.findByRole("button", { name: "Monitor Desk lamp" }));

    await waitFor(() => expect(byId("message")).toHaveTextContent("Request failed. Please retry."));
  });

  it("renders script-shaped device and entity names as literal text", async () => {
    startOn("setup");
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
      "GET /api/events",
    ] as const;
    await waitFor(() => {
      for (const route of routes) expect(api.callsTo(route)).toHaveLength(1);
    });

    await act(() => vi.advanceTimersByTimeAsync(2500));
    expect(api.callsTo("GET /api/catalog")).toHaveLength(1);

    await act(() => vi.advanceTimersByTimeAsync(500));
    await waitFor(() => {
      for (const route of routes) expect(api.callsTo(route)).toHaveLength(2);
    });
  });
});

describe("light control from the dashboard", () => {
  const COMMAND = "POST /api/devices/{entity_id}/command";
  const CONTROL_NOTICE = "Light control is on for monitored lights.";

  /** Monitors the desk lamp (recorded state `state`); control is on unless told otherwise. */
  function renderControl(
    options: {
      control?: Schemas["ControlStatus"];
      state?: Schemas["LightReading"]["state"];
      availability?: Schemas["LightReading"]["availability"];
      light?: Schemas["Entity"];
      outcome?: Schemas["CommandsPage"]["commands"][number]["outcome"];
    } = {},
  ) {
    const light = options.light ?? fixtures.desk;
    return renderDashboard({
      "GET /api/control": options.control ?? fixtures.controlOn,
      "GET /api/catalog": { ...fixtures.catalog, entities: [light, fixtures.reading] },
      "GET /api/selection": { ...fixtures.emptySelection, entity_ids: [light.id] },
      "GET /api/monitored": {
        entities: [fixtures.monitoredLight(light, options.state ?? "off", options.availability)],
      },
      [COMMAND]: fixtures.commandRecord(options.outcome ?? "observed"),
    });
  }

  const monitored = () => within(byId("monitored"));
  const toggles = () => monitored().queryAllByRole("button", { name: /^Turn (on|off) / });

  /** Waits until the dashboard has rendered the light and had time to read the control status. */
  async function loaded() {
    await monitored().findByRole("heading", { name: "Desk lamp" });
    await act(() => vi.advanceTimersByTimeAsync(100));
  }

  it("shows no toggle and the read-only texts when control is off", async () => {
    renderControl({ control: fixtures.controlOff });
    await loaded();

    expect(toggles()).toEqual([]);
    expect(screen.getByText("READ ONLY")).toBeInTheDocument();
    expect(screen.queryByText("CONTROL ON")).toBeNull();
    expect(byId("source-notice")).toHaveTextContent(
      "Demo data only. No hardware or Home Assistant is connected. Light control is off: Home Keeper only reads.",
    );
    expect(byId("source-notice")).not.toHaveTextContent(CONTROL_NOTICE);
  });

  it("treats a failed control read as control off", async () => {
    const { api } = renderControl();
    api.fail("GET /api/control", 500);
    await loaded();

    expect(api.callsTo("GET /api/control").length).toBeGreaterThan(0);
    expect(toggles()).toEqual([]);
    expect(screen.getByText("READ ONLY")).toBeInTheDocument();
    expect(byId("source-notice")).not.toHaveTextContent(CONTROL_NOTICE);
  });

  it("says control is on in the tag and the source notice", async () => {
    renderControl();
    await monitored().findByRole("button", { name: "Turn on Desk lamp" });

    expect(screen.getByText("CONTROL ON")).toBeInTheDocument();
    expect(screen.queryByText("READ ONLY")).toBeNull();
    const notice = byId("source-notice").textContent ?? "";
    expect(notice.endsWith(CONTROL_NOTICE)).toBe(true);
    expect(notice).not.toContain("Light control is off");
    expect(notice).toMatch(/^Demo data only\./);
  });

  it("keeps the Home Assistant notice wording and adds the control sentence when control is on", async () => {
    renderControl();
    api.reply("GET /api/catalog", {
      ...fixtures.catalog,
      source_id: "home_assistant",
      entities: [fixtures.desk, fixtures.reading],
    });
    await waitFor(() => expect(byId("source")).toHaveTextContent("HOME ASSISTANT"));
    await monitored().findByRole("button", { name: "Turn on Desk lamp" });

    expect(byId("source-notice")).toHaveTextContent(
      "Home Assistant discovery. Home Keeper shows only the lights, plugs and switches you select. Light control is on for monitored lights.",
    );
    expect(byId("source-notice")).not.toHaveTextContent("Light control is off");
  });

  it("keeps the footer when control is on", async () => {
    renderControl();
    await monitored().findByRole("button", { name: "Turn on Desk lamp" });

    expect(document.querySelector("footer")).toHaveTextContent(
      "History comes from Home Assistant · Changes show what happened, not why",
    );
  });

  it.each([
    ["on", "Turn off Desk lamp"],
    ["off", "Turn on Desk lamp"],
  ] as const)("names the toggle for a light recorded %s", async (state, name) => {
    renderControl({ state });

    const toggle = await monitored().findByRole("button", { name });
    expect(toggle).toHaveAttribute("type", "button");
    expect(toggle).toBeEnabled();
    expect(toggles()).toHaveLength(1);
  });

  it.each([
    ["unknown state", "unknown", "available"],
    ["unavailable light", "unavailable", "unavailable"],
  ] as const)("shows no toggle for a %s", async (_label, state, availability) => {
    renderControl({ state, availability });
    await loaded();

    expect(toggles()).toEqual([]);
  });

  it("shows no toggle for a light Home Assistant no longer has", async () => {
    renderControl();
    api.reply("GET /api/monitored", { entities: [fixtures.missingDevice(fixtures.desk)] });
    await loaded();
    await nextPoll();

    expect(toggles()).toEqual([]);
  });

  it("sends one command per click with the exact body and mutation header", async () => {
    const { api, user } = renderControl({ state: "off" });

    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));

    await waitFor(() => expect(byId("message").textContent).toBe("Desk lamp is now on."));
    expect(api.callsTo(COMMAND)).toHaveLength(1);
    const [command] = api.callsTo(COMMAND);
    expect(command?.url.pathname).toBe("/api/devices/light.hue_desk/command");
    expect(command?.headers.get("X-Home-Keeper")).toBe("1");
    expect(command?.body).toEqual({ command: { action: "on" }, sender: { kind: "dashboard" } });
  });

  it("sends off for a light recorded on", async () => {
    const { api, user } = renderControl({ state: "on" });

    await user.click(await monitored().findByRole("button", { name: "Turn off Desk lamp" }));

    await waitFor(() => expect(byId("message").textContent).toBe("Desk lamp is now off."));
    expect(api.callsTo(COMMAND).map((call) => call.body)).toEqual([
      { command: { action: "off" }, sender: { kind: "dashboard" } },
    ]);
  });

  it("disables the toggle and says Sending… while the command runs, and ignores a second click", async () => {
    const { api, user } = renderControl({ state: "off" });
    const release = api.hold(COMMAND);

    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));

    const sending = await monitored().findByText("Sending…");
    const button = sending.closest("button");
    expect(button).toBeDisabled();
    await user.click(sending);
    fireEvent.click(sending);
    expect(api.callsTo(COMMAND)).toHaveLength(1);

    release();
    await waitFor(() => expect(byId("message").textContent).toBe("Desk lamp is now on."));
    expect(monitored().queryByText("Sending…")).toBeNull();
    expect(monitored().getByRole("button", { name: "Turn on Desk lamp" })).toBeEnabled();
    expect(api.callsTo(COMMAND)).toHaveLength(1);
  });

  it("still sends a Monitor request while a command runs", async () => {
    const { api, user } = renderControl({ state: "off" });
    const release = api.hold(COMMAND);

    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));
    await monitored().findByText("Sending…");
    openTab("Setup");
    await user.click(within(byId("catalog")).getByRole("button", { name: "Monitor Reading lamp" }));

    await waitFor(() => expect(api.callsTo("PUT /api/selection/{entity_id}")).toHaveLength(1));
    expect(api.callsTo("PUT /api/selection/{entity_id}")[0]?.body).toEqual({ selected: true });
    release();
    await waitFor(() => expect(byId("message").textContent).toBe("Desk lamp is now on."));
  });

  it("still sends a Stop request while a command runs", async () => {
    const { api, user } = renderControl({ state: "off" });
    const release = api.hold(COMMAND);

    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));
    await monitored().findByText("Sending…");
    await user.click(monitored().getByRole("button", { name: "Stop monitoring Desk lamp" }));

    await waitFor(() => expect(api.callsTo("PUT /api/selection/{entity_id}")).toHaveLength(1));
    expect(api.callsTo("PUT /api/selection/{entity_id}")[0]?.body).toEqual({ selected: false });
    release();
  });

  it.each([
    ["not_observed", "Sent to Desk lamp, but no change was seen yet."],
    ["refused", "Home Assistant refused the command for Desk lamp."],
    ["unreachable", "Cannot reach the controller. Desk lamp may not have changed."],
  ] as const)("shows the %s outcome and keeps the recorded state", async (outcome, message) => {
    const { user } = renderControl({ state: "off", outcome });

    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));

    await waitFor(() => expect(byId("message").textContent).toBe(message));
    expect(monitored().getByRole("button", { name: "Turn on Desk lamp" })).toBeEnabled();
    expect(monitored().queryByRole("button", { name: "Turn off Desk lamp" })).toBeNull();
  });

  it("reloads after a command and shows the recorded state, not the requested one", async () => {
    const { api, user } = renderControl({ state: "off", outcome: "observed" });

    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));
    await waitFor(() => expect(byId("message").textContent).toBe("Desk lamp is now on."));
    // The recording still says off, so the toggle still offers on.
    expect(monitored().getByRole("button", { name: "Turn on Desk lamp" })).toBeInTheDocument();

    const reloads = api.callsTo("GET /api/monitored").length;
    api.reply("GET /api/monitored", {
      entities: [fixtures.monitoredLight(fixtures.desk, "on")],
    });
    await nextPoll();
    await waitFor(() => expect(api.callsTo("GET /api/monitored").length).toBeGreaterThan(reloads));
    expect(await monitored().findByRole("button", { name: "Turn off Desk lamp" })).toBeEnabled();
  });

  it("requests the monitored list again right after a command", async () => {
    const { api, user } = renderControl({ state: "off" });
    const button = await monitored().findByRole("button", { name: "Turn on Desk lamp" });
    const before = api.callsTo("GET /api/monitored").length;

    // No timer advance: only a post-command reload can add a request.
    await user.click(button);
    await waitFor(() => expect(byId("message").textContent).toBe("Desk lamp is now on."));

    expect(api.callsTo("GET /api/monitored").length).toBeGreaterThan(before);
  });

  it("shows the request error message when the command call fails", async () => {
    const { api, user } = renderControl({ state: "off" });
    api.fail(COMMAND, 500);

    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));

    await waitFor(() => expect(byId("message")).toHaveTextContent("Request failed. Please retry."));
    expect(monitored().getByRole("button", { name: "Turn on Desk lamp" })).toBeEnabled();
    expect(api.callsTo(COMMAND)).toHaveLength(1);
  });

  it("renders a light name with markup as text in the toggle and the message", async () => {
    const name = "<img src=x onerror=alert('light')>";
    const light = { ...fixtures.desk, device_name: name };
    const { user } = renderControl({ state: "off", light });

    await user.click(await monitored().findByRole("button", { name: `Turn on ${name}` }));

    await waitFor(() => expect(byId("message").textContent).toBe(`${name} is now on.`));
    expect(document.querySelector("img, script")).toBeNull();
  });

  it("keeps a command outcome message through the next background poll", async () => {
    const { api, user } = renderControl({ state: "off", outcome: "refused" });
    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));
    await waitFor(() =>
      expect(byId("message").textContent).toBe("Home Assistant refused the command for Desk lamp."),
    );

    const polls = api.callsTo("GET /api/health").length;
    await nextPoll();
    await waitFor(() => expect(api.callsTo("GET /api/health").length).toBeGreaterThan(polls));

    expect(byId("message").textContent).toBe("Home Assistant refused the command for Desk lamp.");
  });

  it("releases the toggle and shows the outcome while the reload after the command is pending", async () => {
    const { api, user } = renderControl({ state: "off", outcome: "unreachable" });
    const toggle = await monitored().findByRole("button", { name: "Turn on Desk lamp" });
    const releaseReload = api.hold("GET /api/monitored");

    await user.click(toggle);

    await waitFor(() =>
      expect(byId("message").textContent).toBe(
        "Cannot reach the controller. Desk lamp may not have changed.",
      ),
    );
    expect(monitored().queryByText("Sending…")).toBeNull();
    expect(monitored().getByRole("button", { name: "Turn on Desk lamp" })).toBeEnabled();
    releaseReload();
  });

  it("shows the API's refusal text with the light name and reloads", async () => {
    const { api, user } = renderControl({ state: "off" });
    api.fail(COMMAND, {
      status: 409,
      body: { detail: { code: "already_in_state", message: "The light is already on <b>." } },
    });
    const toggle = await monitored().findByRole("button", { name: "Turn on Desk lamp" });
    const before = api.callsTo("GET /api/monitored").length;

    await user.click(toggle);

    await waitFor(() =>
      expect(byId("message").textContent).toBe("Desk lamp: The light is already on <b>."),
    );
    expect(byId("message").querySelector("b")).toBeNull();
    expect(api.callsTo("GET /api/monitored").length).toBeGreaterThan(before);
    expect(monitored().getByRole("button", { name: "Turn on Desk lamp" })).toBeEnabled();
  });

  it("keeps the generic error for HTTP errors without an API refusal", async () => {
    const { api, user } = renderControl({ state: "off" });
    api.fail(COMMAND, { status: 422, body: { detail: [{ msg: "bad" }] } });

    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));

    await waitFor(() => expect(byId("message").textContent).toBe("Request failed. Please retry."));
  });

  it("sends a command for a second light while the first light's command runs", async () => {
    const { api, user } = renderDashboard({
      "GET /api/control": fixtures.controlOn,
      "GET /api/selection": {
        ...fixtures.emptySelection,
        entity_ids: [fixtures.desk.id, fixtures.reading.id],
      },
      "GET /api/monitored": {
        entities: [
          fixtures.monitoredLight(fixtures.desk, "off"),
          fixtures.monitoredLight(fixtures.reading, "on"),
        ],
      },
      [COMMAND]: fixtures.commandRecord("observed"),
    });
    const release = api.hold(COMMAND);

    await user.click(await monitored().findByRole("button", { name: "Turn on Desk lamp" }));
    await user.click(monitored().getByRole("button", { name: "Turn off Reading lamp" }));

    await waitFor(() => expect(api.callsTo(COMMAND)).toHaveLength(2));
    expect(api.callsTo(COMMAND).map((call) => call.url.pathname)).toEqual([
      "/api/devices/light.hue_desk/command",
      "/api/devices/light.hue_reading/command",
    ]);
    release();
  });

  it.each(["off", "network", 500] as const)(
    "turns controls off when the control read says %s and the monitored read fails",
    async (result) => {
      const { api } = renderControl({ state: "off" });
      await monitored().findByRole("button", { name: "Turn on Desk lamp" });
      api.fail("GET /api/monitored", 500);
      if (result === "off") api.reply("GET /api/control", fixtures.controlOff);
      else api.fail("GET /api/control", result);

      await nextPoll();
      await waitFor(() => expect(api.callsTo("GET /api/control")).toHaveLength(2));
      await waitFor(() => expect(byId("message")).toHaveTextContent(UNREACHABLE));

      expect(toggles()).toEqual([]);
      expect(screen.getByText("READ ONLY")).toBeInTheDocument();
      expect(screen.queryByText("CONTROL ON")).toBeNull();
      expect(byId("source-notice").textContent).toBe(
        "Demo data only. No hardware or Home Assistant is connected. Light control is off: Home Keeper only reads.",
      );
    },
  );

  it("does not bring the toggle back when an older poll's control answer arrives late", async () => {
    const { api } = renderControl({ state: "off" });
    await monitored().findByRole("button", { name: "Turn on Desk lamp" });

    // Delay one already-answered control response so it lands after a newer poll.
    const forward = globalThis.fetch;
    let claimed = false;
    let captured = false;
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      const delay = !claimed && new URL(request.url).pathname === "/api/control";
      if (delay) claimed = true;
      const response = await forward(request);
      if (delay) {
        captured = true;
        await gate;
      }
      return response;
    });

    await nextPoll();
    expect(captured).toBe(true);
    api.reply("GET /api/control", fixtures.controlOff);
    await nextPoll();
    await waitFor(() => expect(toggles()).toEqual([]));

    await act(async () => release());

    expect(toggles()).toEqual([]);
    expect(screen.getByText("READ ONLY")).toBeInTheDocument();
  });

  it("keeps the newer light state when an older poll's monitored answer arrives late", async () => {
    const { api } = renderControl({ state: "off" });
    await monitored().findByRole("button", { name: "Turn on Desk lamp" });

    // Delay one already-answered monitored response so it lands after a newer poll.
    const forward = globalThis.fetch;
    let claimed = false;
    let captured = false;
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init);
      const delay = !claimed && new URL(request.url).pathname === "/api/monitored";
      if (delay) claimed = true;
      const response = await forward(request);
      if (delay) {
        captured = true;
        await gate;
      }
      return response;
    });

    await nextPoll();
    expect(captured).toBe(true);
    api.reply("GET /api/monitored", {
      entities: [fixtures.monitoredLight(fixtures.desk, "on")],
    });
    await nextPoll();
    await monitored().findByRole("button", { name: "Turn off Desk lamp" });

    await act(async () => release());

    expect(monitored().getByRole("button", { name: "Turn off Desk lamp" })).toBeInTheDocument();
    expect(byId("message").textContent).not.toBe(UNREACHABLE);
  });

  it("shows the generic error for a dropped command request, then lets a retry work", async () => {
    const { api, user } = renderControl({ state: "off" });
    const button = await monitored().findByRole("button", { name: "Turn on Desk lamp" });
    api.fail(COMMAND, "network");

    await user.click(button);

    await waitFor(() => expect(byId("message").textContent).toBe("Request failed. Please retry."));
    expect(button).toBeEnabled();
    expect(api.callsTo(COMMAND)).toHaveLength(1);

    api.reply(COMMAND, fixtures.commandRecord("observed"));
    await user.click(button);
    await waitFor(() => expect(byId("message").textContent).toBe("Desk lamp is now on."));
    expect(api.callsTo(COMMAND)).toHaveLength(2);
  });
});

describe("command history in the timeline", () => {
  beforeEach(() => startOn("history"));

  const COMMANDS = "GET /api/commands";
  const commandsSection = () => within(byId("commands"));
  const articles = () => commandsSection().getAllByRole("article");
  const { historyCommand: command, commandsPage } = fixtures;

  it("is hidden when control is off and there are no commands", async () => {
    const { api } = renderDashboard({ [COMMANDS]: fixtures.emptyCommands });
    await waitFor(() => expect(api.callsTo("GET /api/events").length).toBeGreaterThan(0));
    await act(() => vi.advanceTimersByTimeAsync(100));

    expect(document.getElementById("commands")).toBeNull();
    expect(screen.queryByText("Device commands")).toBeNull();
    expect(screen.queryByText("No device commands yet.")).toBeNull();
  });

  it("says there are no commands yet when control is on", async () => {
    renderDashboard({ "GET /api/control": fixtures.controlOn });

    expect(await screen.findByText("No device commands yet.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Device commands" })).toBeInTheDocument();
  });

  it("shows commands when control is off but commands exist, newest page of 20", async () => {
    const { api } = renderDashboard({ [COMMANDS]: commandsPage([command("c1")]) });

    await waitFor(() => expect(articles()).toHaveLength(1));
    expect(api.callsTo(COMMANDS)[0]?.url.searchParams.get("limit")).toBe("20");
    expect(screen.queryByText("No device commands yet.")).toBeNull();
  });

  it("renders time, light name, action, sender and outcome for each sender and outcome", async () => {
    const rows = [
      command("c1", { sender: { kind: "dashboard" }, outcome: "observed" }),
      command("c2", { sender: { kind: "browser_chat" }, outcome: "not_observed" }),
      command("c3", {
        sender: { kind: "telegram", id: 4242, name: "Maria" },
        outcome: "refused",
      }),
      command("c4", { outcome: "unreachable" }),
      command("c5", { outcome: "pending" }),
      command("c6", { outcome: "interrupted" }),
    ];
    renderDashboard({ [COMMANDS]: commandsPage(rows) });

    await waitFor(() => expect(articles()).toHaveLength(6));
    const [c1, c2, c3, c4, c5, c6] = articles();
    const time = localTime("2026-10-12T10:01:00+00:00");
    // The row shows a short time; the full local date and time is its tooltip.
    expect(c1?.querySelector("time")).toHaveAttribute("title", time);
    expect(c1).toHaveTextContent("Desk lamp");
    expect(c1).toHaveTextContent("Turn on");
    expect(c1).toHaveTextContent("Dashboard");
    expect(c1).toHaveTextContent("Observed");
    expect(c2).toHaveTextContent("Browser chat");
    expect(c2).toHaveTextContent("Not observed");
    expect(c3).toHaveTextContent("Maria (Telegram 4242)");
    expect(c3).toHaveTextContent("Refused by Home Assistant");
    expect(c4).toHaveTextContent("Controller unreachable");
    expect(c5).toHaveTextContent("Pending");
    expect(c6).toHaveTextContent("Interrupted");
  });

  it("words the action: on, on at a percent, and off", async () => {
    renderDashboard({
      [COMMANDS]: commandsPage([
        command("c1", { command: { action: "on", brightness_pct: 30 } }),
        command("c2", { command: { action: "on" } }),
        command("c3", { command: { action: "off" } }),
      ]),
    });

    await waitFor(() => expect(articles()).toHaveLength(3));
    const [c1, c2, c3] = articles();
    expect(c1).toHaveTextContent("Turn on at 30%");
    expect(c2).toHaveTextContent("Turn on");
    expect(c2).not.toHaveTextContent("Turn on at");
    expect(c3).toHaveTextContent("Turn off");
  });

  it("names the light from the catalog and falls back to the raw entity id", async () => {
    renderDashboard({
      [COMMANDS]: commandsPage([
        command("c1", { entity_id: fixtures.reading.id }),
        command("c2", { entity_id: "light.removed_lamp" }),
      ]),
    });

    await waitFor(() => expect(articles()).toHaveLength(2));
    const [c1, c2] = articles();
    expect(c1).toHaveTextContent("Reading lamp");
    expect(c2).toHaveTextContent("light.removed_lamp");
  });

  it("shows no evidence ID for any command, observed or not", async () => {
    renderDashboard({
      [COMMANDS]: commandsPage([
        command("c1", { outcome: "observed" }),
        command("c2", { outcome: "not_observed" }),
      ]),
    });

    await waitFor(() => expect(articles()).toHaveLength(2));
    const [observed, notObserved] = articles();
    expect(observed).toHaveTextContent("Observed");
    expect(byId("commands")).not.toHaveTextContent("Evidence ID");
    expect(notObserved).not.toHaveTextContent("Evidence ID");
  });

  it("renders a Telegram name with markup as literal text", async () => {
    const name = "<img src=x onerror=attack()> <b>Eve</b>";
    renderDashboard({
      [COMMANDS]: commandsPage([command("c1", { sender: { kind: "telegram", id: 7, name } })]),
    });

    await waitFor(() => expect(articles()).toHaveLength(1));
    expect(articles()[0]).toHaveTextContent(`${name} (Telegram 7)`);
    expect(byId("commands").querySelector("img, b")).toBeNull();
  });

  it("loads older commands, appends them, and drops the button on the last page", async () => {
    const { api, user } = renderDashboard({
      [COMMANDS]: (call) =>
        call.url.searchParams.has("cursor")
          ? commandsPage([command("c3", { sender: { kind: "telegram", id: 9, name: "Old" } })])
          : commandsPage([command("c1"), command("c2")], "cursor-2"),
    });

    await waitFor(() => expect(articles()).toHaveLength(2));
    await user.click(commandsSection().getByRole("button", { name: "Load older commands" }));

    await waitFor(() => expect(articles()).toHaveLength(3));
    expect(articles()[2]).toHaveTextContent("Old (Telegram 9)");
    const older = api.callsTo(COMMANDS).at(-1);
    expect(older?.url.searchParams.get("cursor")).toBe("cursor-2");
    expect(older?.url.searchParams.get("limit")).toBe("20");
    expect(screen.queryByRole("button", { name: "Load older commands" })).toBeNull();
  });

  it("keeps older commands shown across polls", async () => {
    const { api, user } = renderDashboard({
      [COMMANDS]: (call) =>
        call.url.searchParams.has("cursor")
          ? commandsPage([command("c3")])
          : commandsPage([command("c1")], "cursor-2"),
    });
    await waitFor(() => expect(articles()).toHaveLength(1));
    await user.click(commandsSection().getByRole("button", { name: "Load older commands" }));
    await waitFor(() => expect(articles()).toHaveLength(2));

    const catalogRequests = api.callsTo("GET /api/catalog").length;
    await nextPoll();
    await waitFor(() =>
      expect(api.callsTo("GET /api/catalog").length).toBeGreaterThan(catalogRequests),
    );

    expect(articles()).toHaveLength(2);
  });

  it("pages events and commands independently", async () => {
    const { api, user } = renderDashboard({
      "GET /api/events": (call) =>
        call.url.searchParams.has("cursor")
          ? { ...fixtures.eventsPage2, events: [fixtures.change2, fixtures.change1] }
          : fixtures.eventsPage1,
      [COMMANDS]: commandsPage([command("c1")], "cursor-2"),
    });
    await waitFor(() =>
      expect(shownTimes()).toEqual([fixtures.change3.time, fixtures.change2.time]),
    );
    await waitFor(() => expect(articles()).toHaveLength(1));
    const commandRequests = api.callsTo(COMMANDS).length;

    await user.click(screen.getByRole("button", { name: "Load older changes" }));

    await waitFor(() => expect(shownTimes()).toContain(fixtures.change1.time));
    expect(api.callsTo(COMMANDS)).toHaveLength(commandRequests);
    expect(api.callsTo(COMMANDS).every((call) => !call.url.searchParams.has("cursor"))).toBe(true);
    expect(
      commandsSection().getByRole("button", { name: "Load older commands" }),
    ).toBeInTheDocument();
    expect(articles()).toHaveLength(1);
  });

  it("returns to the newest commands on Refresh history and keeps refreshing them", async () => {
    const { api, user } = renderDashboard({
      [COMMANDS]: (call) =>
        call.url.searchParams.has("cursor")
          ? commandsPage([command("c-old")])
          : commandsPage([command("c-new")], "cursor-2"),
    });
    await waitFor(() => expect(articles()).toHaveLength(1));
    await user.click(commandsSection().getByRole("button", { name: "Load older commands" }));
    await waitFor(() => expect(articles()).toHaveLength(2));
    const before = api.callsTo(COMMANDS).length;

    await user.click(screen.getByRole("button", { name: "Refresh history" }));

    await waitFor(() => expect(api.callsTo(COMMANDS).length).toBeGreaterThan(before));
    expect(api.callsTo(COMMANDS).at(-1)?.url.searchParams.has("cursor")).toBe(false);
    await waitFor(() => expect(articles()).toHaveLength(1));
    const afterRefresh = api.callsTo(COMMANDS).length;

    await nextPoll();
    await waitFor(() => expect(api.callsTo(COMMANDS).length).toBeGreaterThan(afterRefresh));
    expect(api.callsTo(COMMANDS).at(-1)?.url.searchParams.has("cursor")).toBe(false);
  });

  it("refreshes the newest commands when Refresh history is clicked during an older request", async () => {
    const { api, user } = renderDashboard({
      [COMMANDS]: (call) =>
        call.url.searchParams.has("cursor")
          ? commandsPage([
              command("c-old", { sender: { kind: "telegram", id: 99, name: "Older" } }),
            ])
          : commandsPage([command("c1")], "cursor-2"),
    });
    await waitFor(() => expect(articles()).toHaveLength(1));
    // Only the older page waits, so it can answer after the refresh.
    const release = api.hold(COMMANDS, (call) => call.url.searchParams.has("cursor"));
    await user.click(commandsSection().getByRole("button", { name: "Load older commands" }));
    await waitFor(() =>
      expect(api.callsTo(COMMANDS).at(-1)?.url.searchParams.has("cursor")).toBe(true),
    );
    api.reply(COMMANDS, (call) =>
      call.url.searchParams.has("cursor")
        ? commandsPage([command("c-old", { sender: { kind: "telegram", id: 99, name: "Older" } })])
        : commandsPage(
            [command("c-new", { sender: { kind: "browser_chat" } }), command("c1")],
            "cursor-2",
          ),
    );

    await user.click(screen.getByRole("button", { name: "Refresh history" }));
    await waitFor(() => expect(articles()).toHaveLength(2));
    // The stale older page lands last, with no poll after it to hide an overwrite.
    await act(async () => {
      release();
      await vi.advanceTimersByTimeAsync(100);
    });

    expect(articles()).toHaveLength(2);
    expect(articles()[0]).toHaveTextContent("Browser chat");
    expect(commandsSection().queryByText(/Older \(Telegram 99\)/)).toBeNull();
    const afterRefresh = api.callsTo(COMMANDS).length;

    await nextPoll();
    await waitFor(() => expect(api.callsTo(COMMANDS).length).toBeGreaterThan(afterRefresh));
    expect(api.callsTo(COMMANDS).at(-1)?.url.searchParams.has("cursor")).toBe(false);
  });

  it("replaces stale command records on the next poll", async () => {
    const { api } = renderDashboard({
      [COMMANDS]: commandsPage([command("c1", { outcome: "pending" })]),
    });
    await waitFor(() => expect(articles()[0]).toHaveTextContent("Pending"));

    api.reply(
      COMMANDS,
      commandsPage([
        command("c2", { sender: { kind: "browser_chat" } }),
        command("c1", { outcome: "observed" }),
      ]),
    );
    await nextPoll();

    await waitFor(() => expect(articles()).toHaveLength(2));
    expect(articles()[0]).toHaveTextContent("Browser chat");
    expect(articles()[1]).toHaveTextContent("Observed");
    expect(articles()[1]).not.toHaveTextContent("Pending");
  });

  it("keeps the last command list when a later read fails", async () => {
    const { api } = renderDashboard({ [COMMANDS]: commandsPage([command("c1")]) });
    await waitFor(() => expect(articles()).toHaveLength(1));

    api.fail(COMMANDS, "network");
    await nextPoll();
    await waitFor(() => expect(api.callsTo(COMMANDS).length).toBeGreaterThan(1));

    expect(articles()).toHaveLength(1);
    expect(byId("timeline-status")).not.toHaveTextContent(HISTORY_FAILED);
    expect(document.body).not.toHaveTextContent(UNREACHABLE);
  });
});

describe("tabs, brightness and plain history", () => {
  const COMMAND = "POST /api/devices/{entity_id}/command";

  /** The desk lamp, monitored and recorded `state`, with control on unless told otherwise. */
  function renderLamp(
    state: Reading["state"] = "on",
    control: Schemas["ControlStatus"] = fixtures.controlOn,
  ) {
    return renderDashboard({
      "GET /api/control": control,
      "GET /api/selection": { ...fixtures.emptySelection, entity_ids: [fixtures.desk.id] },
      "GET /api/monitored": { entities: [fixtures.monitoredLight(fixtures.desk, state)] },
      [COMMAND]: fixtures.commandRecord("observed"),
    });
  }

  const tab = (name: string) => screen.getByRole("tab", { name });
  const slider = () => screen.getByRole("slider", { name: "Brightness of Desk lamp" });

  it("opens on Home and shows only the open tab's view", async () => {
    renderLamp();
    await within(byId("monitored")).findByRole("heading", { name: "Desk lamp" });

    expect(tab("Home")).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("heading", { name: "Choose lights to monitor" })).toBeNull();

    openTab("Setup");

    expect(tab("Setup")).toHaveAttribute("aria-selected", "true");
    expect(window.location.hash).toBe("#setup");
    expect(screen.getByRole("heading", { name: "Choose lights to monitor" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Desk lamp" })).toBeNull();
  });

  it("opens the tab a link names", async () => {
    startOn("history");
    renderLamp();

    await waitFor(() => expect(tab("History")).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByRole("heading", { name: "Recorded changes" })).toBeInTheDocument();
  });

  it("sends one brightness command only after the slider rests for 400 ms", async () => {
    // The 399 ms check needs a clock that only the test moves; with
    // shouldAdvanceTime, real time under a slow run pushes it past 400 ms.
    vi.useFakeTimers();
    const { api } = renderLamp("on");
    await act(() => vi.advanceTimersByTimeAsync(100));

    fireEvent.change(slider(), { target: { value: "60" } });
    await act(() => vi.advanceTimersByTimeAsync(300));
    expect(api.callsTo(COMMAND)).toHaveLength(0);
    // Each move restarts the wait.
    fireEvent.change(slider(), { target: { value: "40" } });
    await act(() => vi.advanceTimersByTimeAsync(399));
    expect(api.callsTo(COMMAND)).toHaveLength(0);
    await act(() => vi.advanceTimersByTimeAsync(1));

    expect(api.callsTo(COMMAND)).toHaveLength(1);
    expect(api.callsTo(COMMAND)[0]?.body).toEqual({
      command: { action: "on", brightness_pct: 40 },
      sender: { kind: "dashboard" },
    });
    // waitFor polls on the faked clock, which no longer moves on its own.
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(byId("message")).toHaveTextContent("Desk lamp is now on at 40%.");
  });

  it("drops a slider move that has not settled when Turn off is clicked", async () => {
    const { api } = renderLamp("on");
    await act(() => vi.advanceTimersByTimeAsync(100));

    fireEvent.change(slider(), { target: { value: "40" } });
    fireEvent.click(screen.getByRole("button", { name: "Turn off Desk lamp" }));
    await act(() => vi.advanceTimersByTimeAsync(1000));

    expect(api.callsTo(COMMAND).map((call) => call.body)).toEqual([
      { command: { action: "off" }, sender: { kind: "dashboard" } },
    ]);
  });

  it("turns an off light on at the slider's brightness", async () => {
    const { api } = renderLamp("off");
    await act(() => vi.advanceTimersByTimeAsync(100));

    fireEvent.change(slider(), { target: { value: "30" } });
    await act(() => vi.advanceTimersByTimeAsync(500));

    await waitFor(() =>
      expect(api.callsTo(COMMAND)[0]?.body).toEqual({
        command: { action: "on", brightness_pct: 30 },
        sender: { kind: "dashboard" },
      }),
    );
  });

  it("shows no slider while control is off", async () => {
    renderLamp("on", fixtures.controlOff);
    await within(byId("monitored")).findByRole("heading", { name: "Desk lamp" });
    await act(() => vi.advanceTimersByTimeAsync(100));

    expect(screen.queryByRole("slider")).toBeNull();
  });

  it("shows the reported brightness as a percent", async () => {
    renderDashboard({
      "GET /api/monitored": {
        entities: [fixtures.monitoredLight(fixtures.desk, "on", "available", 102)],
      },
    });

    expect(await within(byId("monitored")).findByText("On · 40%")).toBeInTheDocument();
  });

  it("leads from an empty Home to Setup", async () => {
    const { user } = renderDashboard({ "GET /api/monitored": fixtures.emptyMonitored });

    await user.click(await screen.findByRole("button", { name: "Choose lights" }));

    expect(tab("Setup")).toHaveAttribute("aria-selected", "true");
  });

  it("names each change by its light and says it in words", async () => {
    startOn("history");
    renderDashboard({
      "GET /api/events": {
        ...fixtures.eventsPage2,
        events: [fixtures.change3, fixtures.change2, fixtures.colorChange],
      },
    });

    const timeline = within(byId("timeline"));
    // 150 of 255 is 59%.
    expect(await timeline.findByText("turned on at 59%")).toBeInTheDocument();
    expect(timeline.getByText("turned off")).toBeInTheDocument();
    expect(timeline.getByText("color changed")).toBeInTheDocument();
    expect(timeline.getAllByText("Desk lamp")).toHaveLength(3);
  });
});

describe("plugs and switches on Home", () => {
  const plugsHeading = () => screen.queryByRole("heading", { name: "Plugs and switches" });

  /** Monitors the desk lamp and the smart plug in the given states, with control on. */
  function renderHome(plug: Schemas["MonitoredEntity"][] = []) {
    return renderDashboard({
      "GET /api/control": fixtures.controlOn,
      "GET /api/catalog": {
        ...fixtures.catalog,
        entities: [fixtures.desk, fixtures.smartPlug, fixtures.ungroupedSensor],
      },
      "GET /api/selection": {
        ...fixtures.emptySelection,
        entity_ids: [fixtures.desk.id, ...plug.map((entity) => entity.id)],
      },
      "GET /api/monitored": {
        entities: [fixtures.monitoredLight(fixtures.desk, "on"), ...plug],
      },
    });
  }

  /** The plug's card: the element around its name heading. */
  async function plugCard() {
    const name = await screen.findByRole("heading", { name: "Smart plug" });
    const card = name.closest("article");
    if (!card) throw new Error("The plug's name is not inside a card");
    return card;
  }

  it("shows a monitored plug's state under the lights, without brightness", async () => {
    renderHome([fixtures.monitoredSwitch(fixtures.smartPlug, "on")]);
    await screen.findByRole("button", { name: "Turn off Desk lamp" });

    const card = await plugCard();
    const lamp = screen.getByRole("heading", { name: "Desk lamp" });
    const heading = screen.getByRole("heading", { name: "Plugs and switches" });
    // Lamp, then the section heading, then the plug card.
    expect(lamp.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(heading.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(card).getByText("On")).toBeInTheDocument();
    expect(within(card).queryByRole("slider")).toBeNull();
    expect(card).not.toHaveTextContent("Brightness");
    expect(card).not.toHaveTextContent("%");
  });

  it("has no plugs section when no switch is monitored", async () => {
    renderHome();
    await screen.findByRole("button", { name: "Turn off Desk lamp" });

    expect(plugsHeading()).toBeNull();
  });

  it("names a plug's change in the timeline among light changes", async () => {
    startOn("history");
    renderHome([fixtures.monitoredSwitch(fixtures.smartPlug, "on")]);
    api.reply("GET /api/events", {
      ...fixtures.eventsPage2,
      events: [fixtures.plugChange, fixtures.change3],
    });

    const timeline = within(byId("timeline"));
    const plug = (await timeline.findByText("Smart plug")).closest("article");
    if (!plug) throw new Error("The plug's change is not a timeline row");
    expect(plug).toHaveTextContent("turned on");
    expect(plug).toHaveTextContent("off → on");
    expect(plug).not.toHaveTextContent("%");
    expect(timeline.getByText("turned on at 59%")).toBeInTheDocument();
  });
});

describe("plug control from the dashboard", () => {
  const COMMAND = "POST /api/devices/{entity_id}/command";
  const PLUG_TOGGLE = /^Turn (on|off) Smart plug$/;

  /** Monitors the desk lamp (on) and the smart plug in `state`; control is on unless told otherwise. */
  function renderPlug(
    options: {
      control?: Schemas["ControlStatus"];
      state?: Schemas["SwitchReading"]["state"];
      availability?: Schemas["SwitchReading"]["availability"];
    } = {},
  ) {
    return renderDashboard({
      "GET /api/control": options.control ?? fixtures.controlOn,
      "GET /api/catalog": { ...fixtures.catalog, entities: [fixtures.desk, fixtures.smartPlug] },
      "GET /api/selection": {
        ...fixtures.emptySelection,
        entity_ids: [fixtures.desk.id, fixtures.smartPlug.id],
      },
      "GET /api/monitored": {
        entities: [
          fixtures.monitoredLight(fixtures.desk, "on"),
          fixtures.monitoredSwitch(
            fixtures.smartPlug,
            options.state ?? "off",
            options.availability,
          ),
        ],
      },
      [COMMAND]: fixtures.switchCommandRecord("observed"),
    });
  }

  /** The plug's card, once the lamp's controls show that the control status has loaded. */
  async function plugCard() {
    await screen.findByRole("heading", { name: "Desk lamp" });
    await act(() => vi.advanceTimersByTimeAsync(100));
    const name = await screen.findByRole("heading", { name: "Smart plug" });
    const card = name.closest("article");
    if (!(card instanceof HTMLElement)) throw new Error("The plug's name is not inside a card");
    return within(card);
  }

  /** The accessible names of the plug's switch buttons that can be clicked now. */
  const enabledToggles = (card: Awaited<ReturnType<typeof plugCard>>) =>
    card
      .queryAllByRole("button", { name: PLUG_TOGGLE })
      .filter((button) => !button.hasAttribute("disabled"))
      .map((button) => button.getAttribute("aria-label") ?? button.textContent);

  it.each([
    ["off", "Turn on Smart plug"],
    ["on", "Turn off Smart plug"],
  ] as const)("offers only the opposite of a plug recorded %s", async (state, name) => {
    renderPlug({ state });
    const card = await plugCard();

    await card.findByRole("button", { name });
    expect(enabledToggles(card)).toEqual([name]);
    expect(card.queryByRole("slider")).toBeNull();
    expect(card.queryByLabelText(/Brightness/)).toBeNull();
  });

  it("shows no plug buttons when control is off", async () => {
    renderPlug({ control: fixtures.controlOff });
    const card = await plugCard();

    expect(card.queryAllByRole("button", { name: PLUG_TOGGLE })).toEqual([]);
  });

  it.each([
    ["unknown state", "unknown", "available"],
    ["unavailable plug", "unavailable", "unavailable"],
  ] as const)("offers no plug switch for an %s", async (_label, state, availability) => {
    renderPlug({ state, availability });
    // The lamp's switch shows that control is on.
    await screen.findByRole("button", { name: "Turn off Desk lamp" });
    const card = await plugCard();

    expect(enabledToggles(card)).toEqual([]);
  });

  it("sends on for a plug recorded off, with no brightness, to the device command route", async () => {
    const { api, user } = renderPlug({ state: "off" });
    const card = await plugCard();

    await user.click(await card.findByRole("button", { name: "Turn on Smart plug" }));

    await waitFor(() => expect(api.callsTo(COMMAND)).toHaveLength(1));
    const [command] = api.callsTo(COMMAND);
    expect(command?.url.pathname).toBe("/api/devices/switch.smart_plug/command");
    expect(command?.headers.get("X-Home-Keeper")).toBe("1");
    expect(command?.body).toEqual({ command: { action: "on" }, sender: { kind: "dashboard" } });
  });

  it("sends off for a plug recorded on", async () => {
    const { api, user } = renderPlug({ state: "on" });
    const card = await plugCard();

    await user.click(await card.findByRole("button", { name: "Turn off Smart plug" }));

    await waitFor(() => expect(api.callsTo(COMMAND)).toHaveLength(1));
    expect(api.callsTo(COMMAND)[0]?.body).toEqual({
      command: { action: "off" },
      sender: { kind: "dashboard" },
    });
  });

  it("disables the plug's buttons while its command runs, and ignores a second click", async () => {
    const { api, user } = renderPlug({ state: "off" });
    const release = api.hold(COMMAND);
    const card = await plugCard();
    const button = await card.findByRole("button", { name: "Turn on Smart plug" });

    await user.click(button);

    await waitFor(() => expect(enabledToggles(card)).toEqual([]));
    expect(card.getAllByRole("button", { name: PLUG_TOGGLE }).length).toBeGreaterThan(0);
    fireEvent.click(button);
    expect(api.callsTo(COMMAND)).toHaveLength(1);

    release();
    await waitFor(() => expect(enabledToggles(card)).toEqual(["Turn on Smart plug"]));
    expect(api.callsTo(COMMAND)).toHaveLength(1);
  });
});

describe("device history", () => {
  const DAY_MS = 24 * 60 * 60 * 1000;
  const monitored = {
    entities: [
      fixtures.monitoredLight(fixtures.desk, "on"),
      fixtures.monitoredSwitch(fixtures.smartPlug, "off"),
    ],
  };
  const HOUR = 3600;
  const deskStrip = (hours: number) => `Last 24 hours: on ${hours}h 0m, unknown 30m`;
  /** The usage fixture with the desk lamp on for `seconds`. */
  const deskOn = (seconds: number) => ({
    ...fixtures.usage,
    devices: fixtures.usage.devices.map((device) =>
      device.entity_id === fixtures.desk.id ? { ...device, on_seconds: seconds } : device,
    ),
  });
  const span = (call: ApiCall | undefined) =>
    Date.parse(call?.url.searchParams.get("end") ?? "") -
    Date.parse(call?.url.searchParams.get("start") ?? "");

  it("draws each card's last 24 hours from the usage periods", async () => {
    const { api } = renderDashboard({
      "GET /api/monitored": monitored,
      "GET /api/usage": fixtures.usage,
    });

    expect(
      await screen.findByRole("img", { name: "Last 24 hours: on 1h 0m, unknown 30m" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Last 24 hours: on 2h 0m, unknown 0m" })).toBeVisible();
    const [call] = api.callsTo("GET /api/usage");
    expect(span(call)).toBe(DAY_MS);
    // Without entity IDs the report covers every monitored device.
    expect(call?.url.searchParams.getAll("entity_id")).toEqual([]);
  });

  it("hatches unavailable time but leaves time without history blank", async () => {
    const unknownIntervals = [
      {
        start: "2026-10-11T10:00:00+00:00",
        end: "2026-10-11T10:30:00+00:00",
        reason: "no_history",
      },
      {
        start: "2026-10-11T12:00:00+00:00",
        end: "2026-10-11T12:30:00+00:00",
        reason: "unavailable",
      },
      {
        start: "2026-10-11T13:00:00+00:00",
        end: "2026-10-11T13:30:00+00:00",
        reason: "unavailable",
      },
    ] satisfies Schemas["UnknownInterval"][];
    renderDashboard({
      "GET /api/monitored": monitored,
      "GET /api/usage": {
        ...fixtures.usage,
        devices: fixtures.usage.devices.map((device) =>
          device.entity_id === fixtures.desk.id
            ? { ...device, unknown_intervals: unknownIntervals }
            : device,
        ),
      },
    });

    const strip = await screen.findByRole("img", { name: "Last 24 hours: on 1h 0m, unknown 30m" });
    const hatched = [...strip.querySelectorAll('[data-segment="unavailable"]')];
    expect(hatched).toHaveLength(2);
    const pattern = strip.querySelector("pattern")?.id;
    expect(pattern).toBeTruthy();
    for (const rect of hatched) expect(rect).toHaveAttribute("fill", `url(#${pattern})`);
  });

  it("reloads the strips a minute later and soon after a card changes, not on every poll", async () => {
    // Each report gives the desk lamp one more hour on, so a stale strip shows.
    const { api } = renderDashboard({
      "GET /api/monitored": monitored,
      "GET /api/usage": () => deskOn(api.callsTo("GET /api/usage").length * HOUR),
    });
    expect(await screen.findByRole("img", { name: deskStrip(1) })).toBeVisible();

    await nextPoll();
    expect(api.callsTo("GET /api/usage")).toHaveLength(1);

    api.reply("GET /api/monitored", {
      entities: [
        fixtures.monitoredLight(fixtures.desk, "off"),
        fixtures.monitoredSwitch(fixtures.smartPlug, "off"),
      ],
    });
    await nextPoll();
    // A change reloads no sooner than 10 s after the last report started.
    await act(() => vi.advanceTimersByTimeAsync(7000));
    expect(await screen.findByRole("img", { name: deskStrip(2) })).toBeVisible();

    await act(() => vi.advanceTimersByTimeAsync(60_000));
    expect(await screen.findByRole("img", { name: deskStrip(3) })).toBeVisible();
    expect(api.callsTo("GET /api/usage")).toHaveLength(3);
  });

  it("hides the strips when a reload fails, so an old day never reads as the last 24 hours", async () => {
    const { api } = renderDashboard({
      "GET /api/monitored": monitored,
      "GET /api/usage": fixtures.usage,
    });
    expect(await screen.findByRole("img", { name: deskStrip(1) })).toBeVisible();

    api.fail("GET /api/usage", 500);
    await act(() => vi.advanceTimersByTimeAsync(60_000));
    await waitFor(() => expect(screen.queryByRole("img", { name: deskStrip(1) })).toBeNull());
  });

  it("opens a device's history, switches the range and closes it", async () => {
    // The 7-day report differs from the 24-hour one, so showing the old range fails.
    const { api, user } = renderDashboard({
      "GET /api/monitored": monitored,
      "GET /api/usage": (call) => deskOn(span(call) === 7 * DAY_MS ? 9 * HOUR : HOUR),
    });
    await user.click(await screen.findByRole("button", { name: "Usage history of Desk lamp" }));

    const dialog = screen.getByRole("dialog", { name: "Desk lamp" });
    expect(await within(dialog).findByRole("img", { name: deskStrip(1) })).toBeVisible();
    expect(within(dialog).getByText("2 times")).toBeVisible();
    expect(within(dialog).getByText("75%")).toBeVisible();
    expect(within(dialog).getByLabelText("10:00, on 1h 0m")).toBeInTheDocument();
    // One day has no per-day chart.
    expect(within(dialog).queryByRole("heading", { name: "On per day" })).toBeNull();
    const forDesk = () =>
      api
        .callsTo("GET /api/usage")
        .filter((call) => call.url.searchParams.getAll("entity_id").includes(fixtures.desk.id));
    expect(span(forDesk()[0])).toBe(DAY_MS);

    const release = api.hold("GET /api/usage");
    await user.click(within(dialog).getByRole("button", { name: "7 days" }));
    expect(within(dialog).getByText("Loading…")).toBeVisible();
    expect(within(dialog).queryByRole("img")).toBeNull();
    release();

    expect(
      await within(dialog).findByRole("img", { name: "Last 7 days: on 9h 0m, unknown 30m" }),
    ).toBeVisible();
    expect(within(dialog).getByRole("heading", { name: "On per day" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "7 days" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(span(forDesk().at(-1))).toBe(7 * DAY_MS);
    expect(within(dialog).getByText("2026-10-11")).toBeVisible();

    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows a plug's history without brightness", async () => {
    const { user } = renderDashboard({
      "GET /api/monitored": monitored,
      "GET /api/usage": fixtures.usage,
    });
    await user.click(await screen.findByRole("button", { name: "Usage history of Smart plug" }));

    const dialog = screen.getByRole("dialog", { name: "Smart plug" });
    expect(await within(dialog).findByText("1 time")).toBeVisible();
    expect(within(dialog).queryByText("Average brightness")).toBeNull();
  });

  it("says when a device's history cannot load, and loads it on retry", async () => {
    const { api, user } = renderDashboard({ "GET /api/monitored": monitored });
    await user.click(await screen.findByRole("button", { name: "Usage history of Desk lamp" }));
    api.fail("GET /api/usage", 500);
    await user.click(screen.getByRole("button", { name: "30 days" }));
    const dialog = screen.getByRole("dialog", { name: "Desk lamp" });
    expect(await within(dialog).findByText(/^Cannot load usage\./)).toBeVisible();

    api.reply("GET /api/usage", fixtures.usage);
    await user.click(within(dialog).getByRole("button", { name: "Retry" }));

    expect(
      await within(dialog).findByRole("img", { name: "Last 30 days: on 1h 0m, unknown 30m" }),
    ).toBeVisible();
  });
});

describe("when Home Assistant cannot be reached", () => {
  const COMMAND = "POST /api/devices/{entity_id}/command";
  // Narrowed, so markup that only claims the button role fails instead of reading as enabled.
  const switchButtons = () =>
    screen
      .queryAllByRole("button", { name: /^Turn (on|off) / })
      .filter((button) => button instanceof HTMLButtonElement);
  const banner = () => screen.queryByText(CANT_REACH_HA);

  /** The desk lamp on and the smart plug off, monitored, with control on. */
  function renderReachable() {
    return renderDashboard({
      "GET /api/control": fixtures.controlOn,
      "GET /api/catalog": { ...fixtures.catalog, entities: [fixtures.desk, fixtures.smartPlug] },
      "GET /api/selection": { entity_ids: [fixtures.desk.id, fixtures.smartPlug.id] },
      "GET /api/monitored": {
        entities: [
          fixtures.monitoredLight(fixtures.desk, "on", "available", 255),
          fixtures.monitoredSwitch(fixtures.smartPlug, "off"),
        ],
      },
      [COMMAND]: fixtures.commandRecord("observed"),
    });
  }

  /** What the API answers while the reader or Home Assistant is down. */
  function loseHomeAssistant(api: FakeApi) {
    api.fail("GET /api/catalog", { status: 503, body: fixtures.unreachableBody });
    api.fail("PUT /api/selection/{entity_id}", { status: 503, body: fixtures.unreachableBody });
    api.fail("GET /api/events", { status: 503, body: fixtures.unreachableBody });
    api.reply("GET /api/health", fixtures.unreachableHealth);
    api.reply("GET /api/monitored", {
      entities: [fixtures.unknownDevice(fixtures.desk), fixtures.unknownDevice(fixtures.smartPlug)],
    });
  }

  function recover(api: FakeApi) {
    api.reply("GET /api/catalog", {
      ...fixtures.catalog,
      entities: [fixtures.desk, fixtures.smartPlug],
    });
    api.reply("PUT /api/selection/{entity_id}", { entity_ids: [fixtures.desk.id] });
    api.reply("GET /api/events", fixtures.emptyEvents);
    api.reply("GET /api/health", fixtures.health);
    api.reply("GET /api/monitored", {
      entities: [
        fixtures.monitoredLight(fixtures.desk, "on", "available", 255),
        fixtures.monitoredSwitch(fixtures.smartPlug, "off"),
      ],
    });
  }

  it("drops the old states, stops switching and warns on every tab, then recovers", async () => {
    const { api } = renderReachable();
    const monitored = within(byId("monitored"));
    expect(await monitored.findByText("On · 100%")).toBeInTheDocument();
    await waitFor(() => expect(switchButtons().length).toBeGreaterThan(0));
    expect(switchButtons().every((button) => !button.disabled)).toBe(true);
    expect(banner()).toBeNull();

    loseHomeAssistant(api);
    await nextPoll();

    // The catalog's 503 must not keep the last snapshot on screen.
    await waitFor(() => expect(banner()).toBeVisible());
    expect(monitored.queryByText("On · 100%")).toBeNull();
    expect(screen.queryByText("Off")).toBeNull();
    expect(monitored.getAllByText("State unknown").length).toBeGreaterThan(0);
    expect(switchButtons().every((button) => button.disabled)).toBe(true);
    expect(screen.queryByRole("slider", { name: /^Brightness of / })).toBeNull();

    for (const name of ["History", "Setup", "Home"] as const) {
      openTab(name);
      expect(banner()).toBeVisible();
    }

    recover(api);
    await nextPoll();

    await waitFor(() => expect(banner()).toBeNull());
    expect(await monitored.findByText("On · 100%")).toBeInTheDocument();
    await waitFor(() => expect(switchButtons().length).toBeGreaterThan(0));
    expect(switchButtons().every((button) => !button.disabled)).toBe(true);
  });

  it("shows unknown states and no switch when a refresh cannot reach Home Keeper", async () => {
    const { api } = renderReachable();
    const monitored = within(byId("monitored"));
    expect(await monitored.findByText("On · 100%")).toBeInTheDocument();
    await waitFor(() => expect(switchButtons().length).toBeGreaterThan(0));

    api.fail("GET /api/monitored", "network");
    await nextPoll();

    await waitFor(() => expect(byId("message")).toHaveTextContent(UNREACHABLE));
    expect(monitored.queryByText("On · 100%")).toBeNull();
    // The lamp and the plug.
    expect(screen.getAllByText("State unknown")).toHaveLength(2);
    expect(switchButtons()).toEqual([]);
  });

  it("shows unknown states when health is unreachable though the states read just before", async () => {
    const { api } = renderReachable();
    const monitored = within(byId("monitored"));
    expect(await monitored.findByText("On · 100%")).toBeInTheDocument();

    // The poll reads its endpoints at once; Home Assistant can go down after /api/monitored.
    api.fail("GET /api/catalog", { status: 503, body: fixtures.unreachableBody });
    api.reply("GET /api/health", fixtures.unreachableHealth);
    await nextPoll();

    await waitFor(() => expect(banner()).toBeVisible());
    expect(monitored.queryByText("On · 100%")).toBeNull();
    expect(screen.getAllByText("State unknown")).toHaveLength(2);
    expect(switchButtons()).toEqual([]);
    expect(screen.queryByRole("slider", { name: /^Brightness of / })).toBeNull();
  });

  it("sends no command while Home Assistant cannot be reached", async () => {
    const { api } = renderReachable();
    await within(byId("monitored")).findByText("On · 100%");

    loseHomeAssistant(api);
    await nextPoll();
    await waitFor(() => expect(banner()).toBeVisible());
    for (const button of switchButtons()) fireEvent.click(button);
    await act(() => vi.advanceTimersByTimeAsync(500));

    expect(api.callsTo(COMMAND)).toEqual([]);
  });

  it("still lists the monitored devices and lets you stop monitoring one", async () => {
    const { api, user } = renderReachable();
    await within(byId("monitored")).findByText("On · 100%");

    loseHomeAssistant(api);
    api.reply("PUT /api/selection/{entity_id}", { entity_ids: [fixtures.smartPlug.id] });
    await nextPoll();
    await waitFor(() => expect(banner()).toBeVisible());

    await user.click(screen.getByRole("button", { name: `Stop monitoring ${fixtures.desk.id}` }));
    const [stop] = api.callsTo("PUT /api/selection/{entity_id}");
    expect(stop?.body).toEqual({ selected: false });
    expect(stop?.url.pathname).toBe(`/api/selection/${fixtures.desk.id}`);
  });

  it("shows no state and no switch for a device Home Assistant no longer has", async () => {
    renderDashboard({
      "GET /api/control": fixtures.controlOn,
      "GET /api/selection": { entity_ids: [fixtures.desk.id] },
      "GET /api/monitored": { entities: [fixtures.missingDevice(fixtures.desk)] },
    });

    const monitored = within(byId("monitored"));
    expect(await monitored.findByRole("heading", { name: "Desk lamp" })).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(monitored.queryByText(/^(On|Off)/)).toBeNull();
    expect(switchButtons()).toEqual([]);
    // Home Assistant answers, so this is no outage.
    expect(banner()).toBeNull();
  });
});

describe("agent panel", () => {
  const STORAGE_KEY = "home-keeper.agent-panel";
  let aegra: FakeAegra;

  /** Renders the dashboard with the keeper API and Aegra behind fakes. */
  function renderWithAgent(replies: Replies = {}) {
    api = installFakeApi(replies);
    aegra = createFakeAegra();
    installFakeAegra(aegra);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const view = render(<App />);
    return { user, view };
  }

  const agentPanel = () =>
    screen.queryByRole("region", { name: "Agent" }) ??
    screen.queryByRole("complementary", { name: "Agent" });

  beforeEach(() => localStorage.clear());
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("is closed on a first visit and opens beside the current tab from Ask", async () => {
    startOn("history");
    const { user } = renderWithAgent();
    await screen.findByRole("tab", { name: "History", selected: true });
    expect(agentPanel()).toBeNull();

    await user.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() => expect(agentPanel()).toBeVisible());
    expect(screen.getByRole("tab", { name: "History", selected: true })).toBeInTheDocument();
    expect(byId("panel-history")).toBeVisible();
  });

  it("remembers that the panel was open or closed across a reload", async () => {
    const first = renderWithAgent();
    await first.user.click(await screen.findByRole("button", { name: "Ask" }));
    await waitFor(() => expect(agentPanel()).toBeVisible());
    first.view.unmount();

    const second = renderWithAgent();
    await waitFor(() => expect(agentPanel()).toBeVisible());
    await second.user.click(screen.getByRole("button", { name: "Close agent panel" }));
    expect(agentPanel()).toBeNull();
    second.view.unmount();

    renderWithAgent();
    await screen.findByRole("button", { name: "Ask" });
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(agentPanel()).toBeNull();
  });

  it.each([
    ["an unreadable saved value", () => localStorage.setItem(STORAGE_KEY, "{not json")],
    [
      "storage that throws",
      () =>
        vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
          throw new DOMException("The operation is insecure.", "SecurityError");
        }),
    ],
  ])("starts closed with %s and still opens", async (_, breakStorage) => {
    breakStorage();
    const { user } = renderWithAgent();

    await user.click(await screen.findByRole("button", { name: "Ask" }));

    await waitFor(() => expect(agentPanel()).toBeVisible());
  });

  it("returns to the tab you were on when you close the panel", async () => {
    startOn("setup");
    const { user } = renderWithAgent();
    await user.click(await screen.findByRole("button", { name: "Ask" }));
    await waitFor(() => expect(agentPanel()).toBeVisible());

    await user.click(screen.getByRole("button", { name: "Close agent panel" }));

    expect(agentPanel()).toBeNull();
    expect(screen.getByRole("tab", { name: "Setup", selected: true })).toBeInTheDocument();
    expect(byId("panel-setup")).toBeVisible();
  });

  it("keeps the dashboard working while the agent is not running", async () => {
    const { user } = renderWithAgent();
    aegra.fail(502);
    await user.click(await screen.findByRole("button", { name: "Ask" }));

    await waitFor(
      () =>
        expect(agentPanel()).toHaveTextContent(
          "The agent isn't running. Start it with --profile agent.",
        ),
      { timeout: 3000 },
    );
    openTab("Setup");
    expect(byId("panel-setup")).toBeVisible();
    expect(await screen.findAllByText(fixtures.desk.device_name)).not.toHaveLength(0);
  });
});
