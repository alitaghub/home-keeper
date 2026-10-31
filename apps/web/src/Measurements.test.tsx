// Measurements on Home and in the History dialog, through the whole dashboard: the cards
// read the day's statistics for their sparkline, and the dialog reads the range picked.
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { type ApiCall, type FakeApi, installFakeApi, type Replies } from "./test/fake-api";
import * as fixtures from "./test/fixtures";

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const { powerMeter } = fixtures;

let api: FakeApi;

function renderDashboard(replies: Replies = {}) {
  api = installFakeApi(replies);
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(<App />);
  return { api, user };
}

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

const span = (call: ApiCall | undefined) =>
  Date.parse(call?.url.searchParams.get("end") ?? "") -
  Date.parse(call?.url.searchParams.get("start") ?? "");

const rangeOf = (call: ApiCall) => ({
  start: call.url.searchParams.get("start") ?? "",
  end: call.url.searchParams.get("end") ?? "",
});

// A day of 5-minute buckets with a gap: three buckets, nothing for about an hour, then two.
// Overall: min 80, mean 180 (100, 200, 300, 200 and 100), max 350.
const DAY_ROWS: [number, number, number, number][] = [
  [120, 100, 80, 120],
  [115, 200, 150, 250],
  [110, 300, 250, 350],
  [60, 200, 100, 300],
  [55, 100, 90, 110],
];
// A week of hourly buckets with a gap. Overall: min 30, mean 276, max 900.
const WEEK_ROWS: [number, number, number, number][] = [
  [30 * 60, 40, 30, 50],
  [29 * 60, 60, 50, 70],
  [28 * 60, 80, 70, 90],
  [10 * 60, 500, 400, 600],
  [9 * 60, 700, 600, 900],
];

/** The power meter's statistics for the range asked: a day's, else a week's. */
function powerReport(call: ApiCall) {
  const range = rangeOf(call);
  const day = span(call) <= DAY_MS;
  const period = day ? "5minute" : "hour";
  return fixtures.measurementReport(range, [
    {
      entity_id: powerMeter.id,
      unit: "W",
      period,
      buckets: fixtures.bucketsBefore(range.end, period, day ? DAY_ROWS : WEEK_ROWS),
      ...(day
        ? { min: 80, mean: 180, max: 350, latest: 412 }
        : { min: 30, mean: 276, max: 900, latest: 412 }),
    },
  ]);
}

type Monitored = ReturnType<
  | typeof fixtures.monitoredLight
  | typeof fixtures.monitoredSwitch
  | typeof fixtures.monitoredState
  | typeof fixtures.monitoredOnOff
>;

const monitored = (...rest: Monitored[]) => ({
  entities: [fixtures.monitoredMeasurement(powerMeter, 412, "W"), ...rest],
});

const section = (name: string) => {
  const found = screen.getByRole("heading", { level: 2, name }).closest("section");
  if (!found) throw new Error(`${name} is not a section`);
  return found;
};

const card = () => {
  const found = within(section("Measurements"))
    .getByRole("heading", { name: powerMeter.name })
    .closest("article");
  if (!found) throw new Error("The power meter is not inside a card");
  return found;
};

describe("measurements on Home", () => {
  it("shows a power meter under Measurements with its value and unit, a 24 hour sparkline and a History button", async () => {
    const { api } = renderDashboard({
      "GET /api/monitored": monitored(),
      "GET /api/measurements": powerReport,
    });

    await screen.findByRole("heading", { level: 2, name: "Measurements" });
    const measurements = section("Measurements");
    expect(measurements.querySelector("#monitored-measurements")).toContainElement(card());
    expect(within(card()).getByText("412 W")).toBeVisible();
    const sparkline = await within(card()).findByRole("img", { name: /^Last 24 hours/ });
    // The accessible name gives the range the line covers and its unit.
    expect(sparkline).toHaveAccessibleName(/^Last 24 hours: .+–.+ W$/);
    expect(within(card()).getByRole("button", { name: /history/i })).toBeVisible();
    expect(within(card()).getByRole("button", { name: /^Stop monitoring/ })).toBeInTheDocument();
    // The statistics asked for cover the last 24 hours.
    const day = api.callsTo("GET /api/measurements").find((call) => span(call) === DAY_MS);
    expect(day).toBeDefined();
  });

  it("draws the sparkline as one line per stretch of buckets, so a gap breaks it", async () => {
    renderDashboard({
      "GET /api/monitored": monitored(),
      "GET /api/measurements": powerReport,
    });

    const sparkline = await within(await waitForCard()).findByRole("img", {
      name: /^Last 24 hours/,
    });

    // Three buckets, a gap, two buckets: two lines, and the first ends before the second starts.
    const lines = [...sparkline.querySelectorAll('polyline[data-segment="mean"]')];
    expect(lines).toHaveLength(2);
    const xs = (line: Element) =>
      (line.getAttribute("points") ?? "")
        .trim()
        .split(/[\s,]+/)
        .filter((_, index) => index % 2 === 0)
        .map(Number);
    const [first, second] = lines.map(xs);
    expect(first?.length).toBeGreaterThan(1);
    expect(second?.length).toBeGreaterThan(1);
    expect(Math.max(...(first ?? []))).toBeLessThan(Math.min(...(second ?? [])));
  });

  it("gives a measurement no command buttons and no dimmer, even with control on", async () => {
    renderDashboard({
      "GET /api/monitored": monitored(),
      "GET /api/measurements": powerReport,
      "GET /api/control": fixtures.controlOn,
    });

    await waitForCard();

    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Turn (on|off)/ })).not.toBeInTheDocument();
  });

  it("shows an unavailable meter with the Unavailable badge instead of a number", async () => {
    renderDashboard({
      "GET /api/monitored": {
        entities: [fixtures.monitoredMeasurement(powerMeter, null, "W", "unavailable")],
      },
      "GET /api/measurements": powerReport,
    });

    const unavailable = await waitForCard();

    expect(unavailable.querySelector("[data-status]")).toHaveTextContent("Unavailable");
    expect(unavailable).not.toHaveTextContent(/null|NaN/);
    expect(within(unavailable).queryByText(/\bW$/)).not.toBeInTheDocument();
  });

  it("orders the sections Lights, Plugs and switches, Other on/off, Measurements, Other", async () => {
    renderDashboard({
      "GET /api/monitored": monitored(
        fixtures.monitoredState(fixtures.thermostat, "heat", "Heat"),
        fixtures.monitoredOnOff(fixtures.frontDoor, "on", "Open"),
        fixtures.monitoredSwitch(fixtures.smartPlug, "on"),
        fixtures.monitoredLight(fixtures.desk, "on"),
      ),
      "GET /api/measurements": powerReport,
    });

    await screen.findByRole("heading", { level: 2, name: "Measurements" });

    expect(
      screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(["Lights", "Plugs and switches", "Other on/off", "Measurements", "Other"]);
  });

  it("shows no Measurements heading when no measurement is monitored", async () => {
    renderDashboard({
      "GET /api/monitored": { entities: [fixtures.monitoredLight(fixtures.desk, "on")] },
    });

    await screen.findByRole("heading", { level: 2, name: "Lights" });

    expect(screen.queryByRole("heading", { name: "Measurements" })).not.toBeInTheDocument();
  });

  it("shows a unit shaped like markup as literal text", async () => {
    const markup = "<img src=x onerror=alert(1)>";
    const { api: fake } = renderDashboard({
      "GET /api/monitored": {
        entities: [fixtures.monitoredMeasurement(powerMeter, 5, markup)],
      },
    });

    await screen.findByRole("heading", { level: 2, name: "Measurements" });

    expect(document.querySelector("img")).toBeNull();
    expect(within(card()).getByText(`5 ${markup}`)).toBeVisible();
    expect(fake.unmatched).toEqual([]);
  });
});

async function waitForCard() {
  await screen.findByRole("heading", { level: 2, name: "Measurements" });
  return card();
}

/** Min, Mean and Max with their values, as the dialog's totals list reads them. */
const totals = (dialog: HTMLElement) =>
  ["Min", "Mean", "Max"].map(
    (label) =>
      [...dialog.querySelectorAll("dl dt")].find((term) => term.textContent === label)
        ?.parentElement?.textContent ?? "",
  );

describe("a measurement's history dialog", () => {
  async function openDialog(replies: Replies = {}) {
    const { api, user } = renderDashboard({
      "GET /api/monitored": monitored(),
      "GET /api/measurements": powerReport,
      ...replies,
    });
    const history = within(await waitForCard()).getByRole("button", { name: /history/i });
    await user.click(history);
    return { api, user, dialog: screen.getByRole("dialog", { name: powerMeter.name }) };
  }

  it("draws a band and a mean line per stretch of buckets and gives Min, Mean and Max with the unit", async () => {
    const { dialog } = await openDialog();

    await waitFor(() => expect(totals(dialog)[0]).toMatch(/Min\s*80 W/));
    const [min, mean, max] = totals(dialog);
    expect(min).toMatch(/Min\s*80 W/);
    expect(mean).toMatch(/Mean\s*180 W/);
    expect(within(dialog).getByRole("img", { name: "24 hours: 80–350 W" })).toBeVisible();
    expect(max).toMatch(/Max\s*350 W/);
    // Each stretch of buckets has its band and its line; nothing is drawn across the gap.
    expect(dialog.querySelectorAll('polygon[data-segment="band"]')).toHaveLength(2);
    expect(dialog.querySelectorAll('polyline[data-segment="mean"]')).toHaveLength(2);
    expect(within(dialog).getByRole("button", { name: "24 hours" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("switches to 7 days and 30 days, asking for each range and showing that range's numbers", async () => {
    const { api, user, dialog } = await openDialog();
    await waitFor(() => expect(totals(dialog)[0]).toMatch(/Min\s*80 W/));
    const forMeter = () =>
      api
        .callsTo("GET /api/measurements")
        .filter((call) => call.url.searchParams.getAll("entity_id").includes(powerMeter.id));
    expect(span(forMeter()[0])).toBe(DAY_MS);

    await user.click(within(dialog).getByRole("button", { name: "7 days" }));

    await waitFor(() => expect(totals(dialog)[0]).toMatch(/Min\s*30 W/));
    const [, mean, max] = totals(dialog);
    expect(mean).toMatch(/Mean\s*276 W/);
    expect(max).toMatch(/Max\s*900 W/);
    expect(span(forMeter().at(-1))).toBe(7 * DAY_MS);
    expect(within(dialog).getByRole("button", { name: "7 days" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // The chart is named by its range and spread.
    expect(within(dialog).getByRole("img", { name: "7 days: 30–900 W" })).toBeVisible();
    // The week has two stretches too, one on each side of the gap.
    expect(dialog.querySelectorAll('polygon[data-segment="band"]')).toHaveLength(2);

    await user.click(within(dialog).getByRole("button", { name: "30 days" }));
    await waitFor(() => expect(span(forMeter().at(-1))).toBe(30 * DAY_MS));
  });

  it("shows an empty chart and a dash for Min, Mean and Max, never zeros, when there are no statistics", async () => {
    const { dialog } = await openDialog({
      "GET /api/measurements": (call) =>
        fixtures.measurementReport(rangeOf(call), [
          {
            entity_id: powerMeter.id,
            unit: "W",
            period: "5minute",
            buckets: [],
            min: null,
            mean: null,
            max: null,
            latest: 412,
          },
        ]),
    });

    await waitFor(() => expect(totals(dialog)[0]).toMatch(/Min\s*—/));
    const [, mean, max] = totals(dialog);
    expect(mean).toMatch(/Mean\s*—/);
    expect(max).toMatch(/Max\s*—/);
    expect(dialog.querySelector("[data-segment]")).toBeNull();
    expect(within(dialog).getByRole("img", { name: "24 hours: no statistics" })).toBeVisible();
    expect(
      within(dialog).getByText("Home Assistant has no statistics for this range."),
    ).toBeVisible();
    expect(dialog).not.toHaveTextContent(/\b0 W/);
  });

  it("closes", async () => {
    const { user, dialog } = await openDialog();

    await user.click(within(dialog).getByRole("button", { name: "Close" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(0));
  });
});

// Means 100, 300, 200 and 400, each bucket reaching 50 below and above its mean, so the series
// runs from 50 to 450.
const WAVE_ROWS: [number, number, number, number][] = [
  [40, 100, 50, 150],
  [35, 300, 250, 350],
  [30, 200, 150, 250],
  [25, 400, 350, 450],
];
const WAVE_MEANS = [100, 300, 200, 400];

function waveReport(call: ApiCall) {
  const range = rangeOf(call);
  return fixtures.measurementReport(range, [
    {
      entity_id: powerMeter.id,
      unit: "W",
      period: "5minute",
      buckets: fixtures.bucketsBefore(range.end, "5minute", WAVE_ROWS),
      min: 50,
      mean: 250,
      max: 450,
      latest: 412,
    },
  ]);
}

/** The vertical positions of a line or area, as the SVG draws them (small is high up). */
const heights = (shape: Element | null | undefined) =>
  (shape?.getAttribute("points") ?? "")
    .trim()
    .split(/\s+/)
    .map((point) => Number(point.split(",")[1]));

/** One height for each bucket, in order: the values of a step line, whatever its points. */
const perBucket = (shape: Element | null | undefined) => [...new Set(heights(shape))];

describe("the vertical scale of a measurement's charts", () => {
  /** Whether a higher value sits higher up: heights fall as `values` rise. */
  const followsValues = (drawn: number[], values: number[]) => {
    expect(drawn).toHaveLength(values.length);
    for (const [index, value] of values.entries()) {
      for (const [other, otherValue] of values.entries()) {
        if (value > otherValue) expect(drawn[index]).toBeLessThan(drawn[other] ?? Number.NaN);
      }
    }
  };

  it("draws the sparkline's mean higher where it is higher", async () => {
    renderDashboard({
      "GET /api/monitored": monitored(),
      "GET /api/measurements": waveReport,
    });

    const sparkline = await within(await waitForCard()).findByRole("img", {
      name: /^Last 24 hours/,
    });

    // A flat scale gives every bucket one height, so four different means must give four.
    followsValues(perBucket(sparkline.querySelector('polyline[data-segment="mean"]')), WAVE_MEANS);
  });

  it("draws the dialog's mean and band on one scale, the band reaching beyond the mean", async () => {
    const { user } = renderDashboard({
      "GET /api/monitored": monitored(),
      "GET /api/measurements": waveReport,
    });
    await user.click(within(await waitForCard()).getByRole("button", { name: /history/i }));
    const dialog = screen.getByRole("dialog", { name: powerMeter.name });

    await waitFor(() => expect(dialog.querySelector('polyline[data-segment="mean"]')).toBeTruthy());

    const mean = perBucket(dialog.querySelector('polyline[data-segment="mean"]'));
    followsValues(mean, WAVE_MEANS);
    // The band runs from the highest maximum (450) to the lowest minimum (50): above the
    // highest mean (400) and below the lowest mean (100).
    const band = heights(dialog.querySelector('polygon[data-segment="band"]'));
    expect(Math.min(...band)).toBeLessThan(Math.min(...mean));
    expect(Math.max(...band)).toBeGreaterThan(Math.max(...mean));
  });
});

describe("the cards' sparklines over time", () => {
  const SPARK = /^Last 24 hours: /;
  const MINUTE_MS = 60_000;

  it("reload after a minute with the new statistics, and not on every 3 second poll", async () => {
    const { api } = renderDashboard({
      "GET /api/monitored": monitored(),
      // Each report reaches one watt higher, so a stale sparkline shows.
      "GET /api/measurements": (call) => {
        const reload = api.callsTo("GET /api/measurements").length;
        const range = rangeOf(call);
        return fixtures.measurementReport(range, [
          {
            entity_id: powerMeter.id,
            unit: "W",
            period: "5minute",
            buckets: fixtures.bucketsBefore(range.end, "5minute", [
              [40, 100, 50 + reload, 150],
              [35, 200, 60, 250 + reload],
            ]),
            min: 50 + reload,
            mean: 150,
            max: 250 + reload,
            latest: 412,
          },
        ]);
      },
    });
    const first = await within(await waitForCard()).findByRole("img", {
      name: "Last 24 hours: 51–251 W",
    });
    expect(first).toBeVisible();

    await act(() => vi.advanceTimersByTimeAsync(3000));
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(api.callsTo("GET /api/measurements")).toHaveLength(1);

    await act(() => vi.advanceTimersByTimeAsync(MINUTE_MS));

    expect(
      await within(card()).findByRole("img", { name: "Last 24 hours: 52–252 W" }),
    ).toBeVisible();
    expect(api.callsTo("GET /api/measurements")).toHaveLength(2);
    expect(within(card()).queryByRole("img", { name: SPARK })).toBeInTheDocument();
  });
});
