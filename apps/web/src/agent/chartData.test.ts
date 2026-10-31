// What a usage_by_day chart draws for each device: on and unknown hours per day, in status
// colors, stacked per device. The rendered tests cannot see bar heights in jsdom.
import { describe, expect, it } from "vitest";
import type { Usage } from "../useUsage";
import { usageDrawing } from "./chartData";
import { parseChartSpec } from "./chartSpec";

const DESK = "light.hue_desk";
const PLUG = "switch.smart_plug";
const START = "2026-10-11T00:00:00Z";
const END = "2026-10-14T00:00:00Z";
const DATES = ["2026-10-11", "2026-10-12", "2026-10-13"];
const HOUR = 3600;

/** A device's usage from its [on, unknown] seconds per day of DATES. */
function deviceUsage(entityId: string, perDay: [on: number, unknown: number][]) {
  const days = DATES.map((date, index) => {
    const [on = 0, unknown = 0] = perDay[index] ?? [];
    return { date, on_seconds: on, unknown_seconds: unknown };
  });
  return {
    device_kind: "switch",
    entity_id: entityId,
    on_seconds: days.reduce((sum, day) => sum + day.on_seconds, 0),
    unknown_seconds: days.reduce((sum, day) => sum + day.unknown_seconds, 0),
    switch_ons: 0,
    days,
    hours: Array.from({ length: 24 }, () => 0),
    unknown_intervals: [{ start: START, end: END, reason: "unavailable" }],
    periods: [{ start: START, end: END, state: "off" }],
  } satisfies Usage["devices"][number];
}

// Every day's on and unknown time differs, within and across devices, so a swap shows.
const usage: Usage = {
  start: START,
  end: END,
  timezone: "Europe/Copenhagen",
  devices: [
    deviceUsage(DESK, [
      [1 * HOUR, 0.25 * HOUR],
      [2 * HOUR, 0.5 * HOUR],
      [3 * HOUR, 0.75 * HOUR],
    ]),
    deviceUsage(PLUG, [
      [4 * HOUR, 1.5 * HOUR],
      [5 * HOUR, 0],
      [0, 2.25 * HOUR],
    ]),
  ],
};

const chart = parseChartSpec({
  spec: { kind: "usage_by_day", entity_ids: [DESK, PLUG], start: START, end: END },
});
if (chart?.kind !== "usage_by_day") throw new Error("The usage_by_day spec did not parse");

const names = new Map([[DESK, "Desk lamp"]]);

describe("usageDrawing", () => {
  const drawing = usageDrawing(chart, usage, (id) => names.get(id) ?? id);
  /** The marks drawn for one device, found by the name it is labelled with. */
  const marksOf = (name: string) => drawing.marks.filter((mark) => mark.label.includes(name));
  const on = (name: string) => marksOf(name).find((mark) => mark.fill.includes("--status-on"));
  const unknown = (name: string) =>
    marksOf(name).find((mark) => mark.fill.includes("--status-unknown"));

  it("draws each device's on and unknown hours per day in the status colors", () => {
    expect(drawing.categories).toHaveLength(DATES.length);
    expect(drawing.marks).toHaveLength(4);
    expect(on("Desk lamp")?.values).toEqual([1, 2, 3]);
    expect(unknown("Desk lamp")?.values).toEqual([0.25, 0.5, 0.75]);
    expect(on(PLUG)?.values).toEqual([4, 5, 0]);
    expect(unknown(PLUG)?.values).toEqual([1.5, 0, 2.25]);
  });

  it("stacks a device's on and unknown time together, apart from the other device", () => {
    const deskStack = on("Desk lamp")?.stack;
    const plugStack = on(PLUG)?.stack;

    expect(deskStack).toBeTruthy();
    expect(plugStack).toBeTruthy();
    expect(unknown("Desk lamp")?.stack).toBe(deskStack);
    expect(unknown(PLUG)?.stack).toBe(plugStack);
    expect(plugStack).not.toBe(deskStack);
  });
});
