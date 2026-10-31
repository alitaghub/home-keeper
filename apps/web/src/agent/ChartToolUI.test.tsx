// The agent's show_chart calls as the panel draws them. Every test goes through the real
// panel, so it also proves the renderer is registered for show_chart and loads on demand.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Schemas } from "../api/client";
import { createFakeAegra, type FakeAegra, installFakeAegra, type Step } from "../test/fake-aegra";
import { type FakeApi, installFakeApi } from "../test/fake-api";
import AgentPanel from "./AgentPanel";

type Usage = Schemas["UsageReport"];
type User = ReturnType<typeof userEvent.setup>;

// The first chart in a test file loads the renderer and Recharts on demand, which takes
// longer than testing-library's 1 s default while the whole suite runs in parallel.
const LOADED = { timeout: 5000 };

const CANNOT_DRAW = "The agent sent a chart the panel cannot draw.";
const LOAD_FAILED = /Could not load the chart data/;
const DESK = "light.hue_desk";
const PLUG = "switch.smart_plug";
// The plug has no friendly name, so the panel falls back to its entity ID.
const NAMES: ReadonlyMap<string, string> = new Map([[DESK, "Desk lamp"]]);

const BAR = {
  kind: "bar",
  title: "Switch-ons this week",
  unit: "times",
  categories: ["Desk lamp", "Reading light", "Kettle plug", "Hall"],
  series: [
    { name: "This week", values: [14, 6, 21, 9] },
    { name: "Last week", values: [11, 8, 19, 12] },
  ],
};
const LINE = {
  kind: "line",
  title: "Desk lamp on-time by hour",
  unit: "min",
  categories: ["06:00", "07:00", "08:00", "09:00", "10:00"],
  series: [{ name: "Desk lamp", values: [5, 30, 45, 20, 10] }],
};
const START = "2026-10-11T00:00:00Z";
const END = "2026-10-13T00:00:00Z";
const DATES = ["2026-10-11", "2026-10-12"];
const usageSpec = (entityIds: string[]) => ({
  kind: "usage_by_day",
  entity_ids: entityIds,
  start: START,
  end: END,
});

const HOUR = 3600;
const DAY = 24 * HOUR;

/** A device's usage from its [on, unknown] seconds per day of DATES. */
function deviceUsage(
  entityId: string,
  perDay: [on: number, unknown: number][],
  reason: "no_history" | "unavailable" = "unavailable",
): Usage["devices"][number] {
  const days = DATES.map((date, index) => {
    const [on = 0, unknown = 0] = perDay[index] ?? [];
    return { date, on_seconds: on, unknown_seconds: unknown };
  });
  const unknownSeconds = days.reduce((sum, day) => sum + day.unknown_seconds, 0);
  return {
    device_kind: "switch",
    entity_id: entityId,
    on_seconds: days.reduce((sum, day) => sum + day.on_seconds, 0),
    unknown_seconds: unknownSeconds,
    switch_ons: 0,
    days,
    hours: Array.from({ length: 24 }, () => 0),
    unknown_intervals: unknownSeconds ? [{ start: START, end: END, reason }] : [],
    periods: [],
  };
}

const report = (...devices: Usage["devices"]): Usage => ({
  start: START,
  end: END,
  timezone: "Europe/Copenhagen",
  devices,
});

// Desk lamp: on 1h 0m, unknown 30m. Plug: on 2h 0m, unknown 45m.
const deskUsage = deviceUsage(DESK, [
  [HOUR / 2, HOUR / 2],
  [HOUR / 2, 0],
]);
const plugUsage = deviceUsage(PLUG, [
  [2 * HOUR, 0],
  [0, 0.75 * HOUR],
]);

/** Answers /api/usage with the devices of `usage` the request names, as the keeper does. */
const usageReply = (usage: Usage) => (call: { url: URL }) => {
  const ids = call.url.searchParams.getAll("entity_id");
  return { ...usage, devices: usage.devices.filter((device) => ids.includes(device.entity_id)) };
};

const chartCall = (spec: unknown, status: "success" | "error" = "success"): Step => ({
  tool: { name: "show_chart", args: { spec } },
  result: "Chart shown.",
  status,
});

let api: FakeApi;
let aegra: FakeAegra;

/** The panel, whose agent answers any question with `steps` and then a short reply. */
function renderChat(steps: Step[], { usage = report(deskUsage, plugUsage), names = NAMES } = {}) {
  api = installFakeApi({ "GET /api/usage": usageReply(usage) });
  aegra = createFakeAegra({
    onRun: ({ question }) => (question ? [...steps, { say: "Here it is." }] : []),
  });
  installFakeAegra(aegra);
  return mountPanel(names);
}

function mountPanel(names: ReadonlyMap<string, string> = NAMES) {
  const user = userEvent.setup();
  const view = render(
    <StrictMode>
      <AgentPanel names={names} onClose={() => {}} />
    </StrictMode>,
  );
  return { user, view };
}

const panel = () =>
  screen.queryByRole("region", { name: "Agent" }) ??
  screen.getByRole("complementary", { name: "Agent" });

async function ask(user: User, question = "Show me a chart") {
  const input = await waitFor(() => within(panel()).getByRole("textbox"));
  await user.type(input, `${question}{Enter}`);
}

/**
 * The chart whose accessible name mentions `text`, with that name. A chart is a figure or
 * an image named with a short summary of what it shows.
 */
function findChart(text: string) {
  return waitFor(() => {
    let summary = "";
    const named = (name: string) => {
      if (!name.includes(text)) return false;
      summary = name;
      return true;
    };
    const [chart] = [
      ...within(panel()).queryAllByRole("figure", { name: named }),
      ...within(panel()).queryAllByRole("img", { name: named }),
    ];
    if (!chart) throw new Error(`No chart named with "${text}" in the panel`);
    return { chart, summary };
  }, LOADED);
}

/** Recharts' drawing surface, present only while the card shows the chart, not the table. */
const surface = () => panel().querySelector("svg.recharts-surface");
/** One layer per Bar series Recharts draws. */
const barLayers = () => [...panel().querySelectorAll(".recharts-bar")];

/** The innermost element whose text has every part, such as one device's totals. */
function innermost(...parts: string[]): HTMLElement | undefined {
  const has = (element: Element) =>
    parts.every((part) => (element.textContent ?? "").includes(part));
  return [...panel().querySelectorAll<HTMLElement>("*")]
    .filter(has)
    .find((element) => ![...element.children].some(has));
}

/** Picks an item from the chart card's "⋯" menu. */
async function choose(user: User, item: string) {
  await user.click(within(panel()).getByRole("button", { name: /options/i }));
  await user.click(await screen.findByRole("menuitem", { name: item }));
}

/** The rows of the card's table as cell texts, header row first. */
function tableRows(): string[][] {
  const table = within(panel()).getByRole("table");
  return within(table)
    .getAllByRole("row")
    .map((row) =>
      [...row.querySelectorAll("th, td")].map((cell) => (cell.textContent ?? "").trim()),
    );
}

/** A minimal RFC 4180 reader: quoted cells, doubled quotes, CRLF or LF rows. */
function parseCsv(text: string): string[][] {
  const body = text.replace(/^﻿/, "");
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < body.length; index += 1) {
    const char = body.charAt(index);
    if (quoted) {
      if (char === '"' && body.charAt(index + 1) === '"') {
        cell += '"';
        index += 1;
      } else if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && body.charAt(index + 1) === "\n") index += 1;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else cell += char;
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

type Download = { name: string; text: () => Promise<string> };

/** Records the files the panel saves through a blob or data link, instead of navigating. */
function captureDownloads(): Download[] {
  const blobs = new Map<string, Blob>();
  const downloads: Download[] = [];
  // jsdom has no object URLs.
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: (blob: Blob) => {
      const url = `blob:chart-test/${blobs.size + 1}`;
      blobs.set(url, blob);
      return url;
    },
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: () => {} });
  const read = async (href: string) => {
    const blob = blobs.get(href);
    if (blob) return blob.text();
    const [head = "", data = ""] = href.split(/,(.*)/s);
    return head.endsWith(";base64") ? atob(data) : decodeURIComponent(data);
  };
  const record = (anchor: HTMLAnchorElement) =>
    downloads.push({ name: anchor.download, text: () => read(anchor.href) });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function click(
    this: HTMLAnchorElement,
  ) {
    if (this.download) record(this);
  });
  const onClick = (event: MouseEvent) => {
    if (event.target instanceof HTMLAnchorElement && event.target.download) {
      event.preventDefault();
      record(event.target);
    }
  };
  document.addEventListener("click", onClick, true);
  cleanups.push(() => {
    document.removeEventListener("click", onClick, true);
    Reflect.deleteProperty(URL, "createObjectURL");
    Reflect.deleteProperty(URL, "revokeObjectURL");
  });
  return downloads;
}

const cleanups: (() => void)[] = [];
const getBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;

beforeEach(() => {
  localStorage.clear();
  // jsdom does no layout: give Recharts' responsive container a size, so charts draw.
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function boundingClientRect(this: HTMLElement) {
      return this.classList.contains("recharts-responsive-container")
        ? DOMRect.fromRect({ width: 480, height: 224 })
        : getBoundingClientRect.call(this);
    },
  );
});

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("show_chart in the agent panel", () => {
  it("shows a placeholder while the chart's arguments stream, then the chart", async () => {
    const { user } = renderChat([chartCall(BAR)]);
    const release = aegra.pauseNextStream();

    await ask(user);

    // The call has arrived with half its arguments.
    await waitFor(() => {
      const placeholder = [
        ...within(panel()).queryAllByRole("status"),
        ...panel().querySelectorAll('[aria-busy="true"]'),
      ].find((element) =>
        /chart/i.test(`${element.textContent} ${element.getAttribute("aria-label") ?? ""}`),
      );
      expect(placeholder).toBeDefined();
    }, LOADED);
    expect(within(panel()).queryByText(CANNOT_DRAW)).toBeNull();
    expect(surface()).toBeNull();
    release();
    await findChart(BAR.title);
    expect(within(panel()).queryByText(CANNOT_DRAW)).toBeNull();
  });

  it("draws a bar chart with its title, series and unit, named with a summary", async () => {
    const { user } = renderChat([chartCall(BAR)]);

    await ask(user);

    const { summary } = await findChart(BAR.title);
    // More than the title: what the chart shows.
    expect(
      [...BAR.categories, ...BAR.series.map((s) => s.name)].some((part) => summary.includes(part)),
    ).toBe(true);
    expect(within(panel()).getByText(BAR.title)).toBeVisible();
    expect(within(panel()).getByText("This week")).toBeVisible();
    expect(within(panel()).getByText("Last week")).toBeVisible();
    expect(panel()).toHaveTextContent(/times/);
    expect(barLayers()).toHaveLength(2);
    // The dashboard's CSP blocks inline <style>; colors must not need one.
    expect(panel().querySelector("style")).toBeNull();
    expect(within(panel()).queryByText(CANNOT_DRAW)).toBeNull();
  });

  it("draws a line chart with straight segments between its points", async () => {
    const { user } = renderChat([chartCall(LINE)]);

    await ask(user);

    await findChart(LINE.title);
    const lines = [...panel().querySelectorAll(".recharts-line-curve")];
    expect(lines).toHaveLength(1);
    // A smoothed curve uses cubic Bézier commands; straight segments use only moves and lines.
    for (const line of lines) expect(line.getAttribute("d")).toMatch(/^M[\d.,\sML-]+$/);
    expect(panel().querySelector("style")).toBeNull();
  });

  it.each([
    ["a spec it cannot parse", chartCall({ ...BAR, color: "#ff0000" })],
    [
      "a spec whose series misses values",
      chartCall({ ...BAR, series: [{ name: "s", values: [1] }] }),
    ],
    ["a valid spec whose tool result is an error", chartCall(BAR, "error")],
  ])("says it cannot draw %s", async (_case, step) => {
    const { user } = renderChat([step]);

    await ask(user);

    expect(await within(panel()).findByText(CANNOT_DRAW, {}, LOADED)).toBeVisible();
    await within(panel()).findByText("Here it is.");
    expect(surface()).toBeNull();
  });

  it("draws on and unknown time per day for one device from the usage it fetches for the range", async () => {
    const { user } = renderChat([chartCall(usageSpec([DESK]))]);

    await ask(user);

    const { summary } = await findChart("Desk lamp");
    expect(summary.length).toBeGreaterThan("Desk lamp".length);
    // The browser asked the keeper for exactly the spec's devices and range.
    const [call] = api.callsTo("GET /api/usage");
    expect(call?.url.searchParams.getAll("entity_id")).toEqual([DESK]);
    expect(Date.parse(call?.url.searchParams.get("start") ?? "")).toBe(Date.parse(START));
    expect(Date.parse(call?.url.searchParams.get("end") ?? "")).toBe(Date.parse(END));
    // On stacked over unknown, and the totals under the chart.
    expect(barLayers()).toHaveLength(2);
    expect(innermost("1h 0m", "30m")).toBeDefined();
    // No caption about where the numbers come from.
    expect(panel()).not.toHaveTextContent(/Home Assistant|source/i);
    expect(panel().querySelector("style")).toBeNull();
  });

  it("shows on and unknown time and its own totals for each of two devices", async () => {
    const { user } = renderChat([chartCall(usageSpec([DESK, PLUG]))]);

    await ask(user);

    await findChart("Desk lamp");
    // One stack per device, each with on and unknown.
    expect(barLayers()).toHaveLength(4);
    const desk = innermost("Desk lamp", "1h 0m", "30m");
    const plug = innermost(PLUG, "2h 0m", "45m");
    expect(desk).toBeDefined();
    expect(plug).toBeDefined();
    expect(desk?.textContent).not.toContain(PLUG);
    expect(plug?.textContent).not.toContain("Desk lamp");
  });

  it("says the data could not load, and draws the chart once Retry loads it", async () => {
    const { user } = renderChat([chartCall(usageSpec([DESK]))]);
    // Set after renderChat installs the fake API.
    api.fail("GET /api/usage", 500);

    await ask(user);

    expect(await within(panel()).findByText(LOAD_FAILED, {}, LOADED)).toBeVisible();
    expect(surface()).toBeNull();
    const failed = api.callsTo("GET /api/usage").length;
    api.reply("GET /api/usage", usageReply(report(deskUsage)));

    await user.click(within(panel()).getByRole("button", { name: "Retry" }));

    await findChart("Desk lamp");
    expect(api.callsTo("GET /api/usage").length).toBeGreaterThan(failed);
    expect(within(panel()).queryByText(LOAD_FAILED)).toBeNull();
    expect(innermost("1h 0m", "30m")).toBeDefined();
  });

  it("draws a range without history as Unknown and says no history is recorded", async () => {
    const empty = deviceUsage(
      DESK,
      [
        [0, DAY],
        [0, DAY],
      ],
      "no_history",
    );
    const { user } = renderChat([chartCall(usageSpec([DESK]))], { usage: report(empty) });

    await ask(user);

    await findChart("Desk lamp");
    expect(surface()).not.toBeNull();
    expect(barLayers().length).toBeGreaterThan(0);
    expect(panel()).toHaveTextContent(/no history/i);
    expect(within(panel()).queryByText(LOAD_FAILED)).toBeNull();
    expect(within(panel()).queryByText(CANNOT_DRAW)).toBeNull();
  });

  it("switches a bar chart to a table of its values and back", async () => {
    const { user } = renderChat([chartCall(BAR)]);
    await ask(user);
    await findChart(BAR.title);

    await choose(user, "Show as table");

    expect(surface()).toBeNull();
    const [header, ...rows] = tableRows();
    // The category column, then one column per series.
    expect(header).toHaveLength(3);
    expect(header?.slice(1)).toEqual(["This week", "Last week"]);
    expect(rows.map((row) => row[0])).toEqual(BAR.categories);
    expect(rows[0]?.[1]).toMatch(/^14\s?times$/);
    expect(rows[2]?.[2]).toMatch(/^19\s?times$/);

    await choose(user, "Show as chart");

    expect(within(panel()).queryByRole("table")).toBeNull();
    expect(surface()).not.toBeNull();
  });

  it("shows a usage table with on and unknown time per device for each day", async () => {
    const { user } = renderChat([chartCall(usageSpec([DESK, PLUG]))]);
    await ask(user);
    await findChart("Desk lamp");

    await choose(user, "Show as table");

    const rows = tableRows();
    const header = rows.slice(0, -DATES.length).flat().join(" ");
    expect(header).toContain("Desk lamp");
    expect(header).toContain(PLUG);
    // The date, then on and unknown time for the desk lamp and then the plug.
    expect(rows.slice(-DATES.length)).toEqual([
      [DATES[0], "30m", "30m", "2h 0m", "0m"],
      [DATES[1], "30m", "0m", "0m", "45m"],
    ]);
  });

  it("downloads the bar chart's values as CSV with a header row and raw numbers", async () => {
    const downloads = captureDownloads();
    const { user } = renderChat([chartCall(BAR)]);
    await ask(user);
    await findChart(BAR.title);

    await choose(user, "Download CSV");

    await waitFor(() => expect(downloads).toHaveLength(1));
    const [csv] = downloads;
    expect(csv?.name).toMatch(/^switch-ons-this-week-\d{4}-\d{2}-\d{2}\.csv$/);
    const [header, ...rows] = parseCsv((await csv?.text()) ?? "");
    expect(header?.slice(1)).toEqual(["This week", "Last week"]);
    expect(rows).toEqual([
      ["Desk lamp", "14", "11"],
      ["Reading light", "6", "8"],
      ["Kettle plug", "21", "19"],
      ["Hall", "9", "12"],
    ]);
  });

  it("downloads usage as CSV in hours with two decimals", async () => {
    const downloads = captureDownloads();
    const { user } = renderChat([chartCall(usageSpec([DESK, PLUG]))]);
    await ask(user);
    await findChart("Desk lamp");

    await choose(user, "Download CSV");

    await waitFor(() => expect(downloads).toHaveLength(1));
    const [header, ...rows] = parseCsv((await downloads[0]?.text()) ?? "");
    expect(header).toHaveLength(5);
    expect(rows).toEqual([
      [DATES[0], "0.50", "0.50", "2.00", "0.00"],
      [DATES[1], "0.50", "0.00", "0.00", "0.75"],
    ]);
  });

  it("shows names as text in the legend and table, and escapes formulas only in the CSV", async () => {
    const downloads = captureDownloads();
    const spec = {
      kind: "bar",
      title: "Names",
      categories: ["=SUM(A1)", "-2+3", "Hall"],
      series: [
        { name: "<b>x</b>", values: [1, 2, 3] },
        { name: "@risk", values: [4, 5, 6] },
      ],
    };
    const { user } = renderChat([chartCall(spec)]);
    await ask(user);
    await findChart("Names");

    expect(within(panel()).getByText("<b>x</b>")).toBeVisible();
    expect(panel().querySelector("b, i, script")).toBeNull();
    await choose(user, "Show as table");
    const [header, ...rows] = tableRows();
    expect(header?.slice(1)).toEqual(["<b>x</b>", "@risk"]);
    expect(rows.map((row) => row[0])).toEqual(["=SUM(A1)", "-2+3", "Hall"]);
    expect(panel().querySelector("b, i, script")).toBeNull();

    await choose(user, "Download CSV");

    await waitFor(() => expect(downloads).toHaveLength(1));
    const [csvHeader, ...csvRows] = parseCsv((await downloads[0]?.text()) ?? "");
    expect(csvHeader?.slice(1)).toEqual(["<b>x</b>", "'@risk"]);
    expect(csvRows.map((row) => row[0])).toEqual(["'=SUM(A1)", "'-2+3", "Hall"]);
  });

  it("shows a device name with markup as text", async () => {
    const names = new Map([[DESK, "<b>x</b>"]]);
    const { user } = renderChat([chartCall(usageSpec([DESK]))], { names });

    await ask(user);

    await findChart("<b>x</b>");
    expect(innermost("<b>x</b>", "1h 0m", "30m")).toBeDefined();
    expect(panel().querySelector("b")).toBeNull();
  });

  it("still shows other tools' cards and approvals beside a chart", async () => {
    const { user } = renderChat([
      chartCall(BAR),
      { tool: { name: "list_monitored", args: {} }, result: "Control: on\n- light.hue_desk: on" },
      { propose: [{ name: "monitor", args: { entity_id: DESK } }] },
    ]);

    await ask(user);

    await findChart(BAR.title);
    expect(within(panel()).getByRole("button", { name: /list_monitored/ })).toBeVisible();
    const approve = await screen.findByRole("button", {
      name: "Approve: Start monitoring Desk lamp",
    });
    await user.click(approve);
    await waitFor(() => expect(aegra.runs).toHaveLength(2));
    expect(aegra.runs[1]?.body.command).toEqual({ resume: { decisions: [{ type: "approve" }] } });
  });

  it("fetches usage again when a chat reopens, and draws the new numbers", async () => {
    const first = renderChat([chartCall(usageSpec([DESK]))], { usage: report(deskUsage) });
    await ask(first.user);
    await findChart("Desk lamp");
    expect(innermost("1h 0m", "30m")).toBeDefined();
    first.view.unmount();
    const before = api.callsTo("GET /api/usage").length;
    // Home Assistant purged the first day: it is unknown now.
    const purged = deviceUsage(DESK, [
      [0, DAY],
      [HOUR / 2, 0],
    ]);
    api.reply("GET /api/usage", usageReply(report(purged)));

    // A reload: the same browser storage, a fresh React tree, the same saved thread.
    mountPanel();

    await waitFor(
      () => expect(api.callsTo("GET /api/usage").length).toBeGreaterThan(before),
      LOADED,
    );
    await waitFor(() => expect(innermost("Desk lamp", "30m", "24h 0m")).toBeDefined(), LOADED);
    expect(innermost("1h 0m", "30m")).toBeUndefined();
  });
});
