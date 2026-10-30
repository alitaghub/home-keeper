import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../api/client";
import { UsageStrip } from "./History";

type LightUsage = Schemas["LightUsage"];

const START = Date.parse("2026-10-11T00:00:00Z");
const HOUR_MS = 60 * 60 * 1000;
const at = (hours: number) => new Date(START + hours * HOUR_MS).toISOString();
// Ten hours across the strip's 1000 units: one hour is 100 units.
const range = { start: at(0), end: at(10) };

const light = (
  periods: LightUsage["periods"],
  unknown_intervals: LightUsage["unknown_intervals"] = [],
): LightUsage => ({
  device_kind: "light",
  entity_id: "light.hue_desk",
  on_seconds: 0,
  unknown_seconds: 0,
  switch_ons: 0,
  average_brightness: null,
  days: [],
  hours: Array.from({ length: 24 }, () => 0),
  unknown_intervals,
  periods,
});

const geometry = (container: HTMLElement, selector: string) =>
  [...container.querySelectorAll(selector)].map((rect) =>
    ["x", "width", "y", "height"].map(
      (name) => Math.round(Number(rect.getAttribute(name) ?? 0) * 1000) / 1000,
    ),
  );

describe("usage strip", () => {
  it("draws on as tall as its brightness, off as a thin line, and hatches unavailable time", () => {
    const device = light(
      [
        { start: at(0), end: at(1), state: "on", brightness: 255 },
        { start: at(1), end: at(2), state: "off", brightness: null },
        { start: at(3), end: at(4), state: "on", brightness: 102 },
      ],
      [{ start: at(2), end: at(3), reason: "unavailable" }],
    );
    const { container } = render(<UsageStrip range={range} device={device} caption="Day" />);

    expect(geometry(container, '[data-segment="on"]')).toEqual([
      [0, 100, 0, 24],
      // 102 / 255 is 40% of the height.
      [300, 100, 14.4, 9.6],
    ]);
    expect(geometry(container, '[data-segment="off"]')).toEqual([[100, 100, 22, 2]]);
    expect(geometry(container, '[data-segment="unavailable"]')).toEqual([[200, 100, 0, 24]]);
    // A hatch, not a solid or empty fill.
    const pattern = container.querySelector("svg pattern")?.id;
    expect(pattern).toBeTruthy();
    expect(container.querySelector('[data-segment="unavailable"]')).toHaveAttribute(
      "fill",
      `url(#${pattern})`,
    );
  });

  it("joins changes narrower than one unit, so a busy month draws about one bar per unit", () => {
    const minutes = 10 * 60 * 100;
    // A light that switches every 6 seconds for 10 hours: 6000 periods per 1000 units.
    const periods = Array.from({ length: minutes }, (_, index) => ({
      start: at(index / 6000),
      end: at((index + 1) / 6000),
      state: index % 2 ? ("off" as const) : ("on" as const),
      brightness: index % 2 ? null : 255,
    }));
    const { container } = render(
      <UsageStrip range={range} device={light(periods)} caption="Busy" />,
    );

    const bars = geometry(container, '[data-segment="on"], [data-segment="off"]');
    expect(bars.length).toBeGreaterThan(0);
    expect(bars.length).toBeLessThanOrEqual(1001);
    // Any on time within a joined bar shows it as on.
    expect(container.querySelectorAll('[data-segment="off"]')).toHaveLength(0);
  });
});
