// What text a chart's PNG paints. jsdom has neither a canvas nor SVG layout, so a recording
// 2D context and minimal SVG geometry stand in at that boundary; the exporter and the card
// are the real ones. Pixels, theme and downloads are proved in the Playwright spec.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Schemas } from "../api/client";
import { createFakeAegra, installFakeAegra } from "../test/fake-aegra";
import { installFakeApi } from "../test/fake-api";
import AgentPanel from "./AgentPanel";
import { chartPng } from "./chartPng";

type Usage = Schemas["UsageReport"];

const DESK = "light.hue_desk";
const PLUG = "switch.smart_plug";
const START = "2026-10-11T00:00:00Z";
const END = "2026-10-12T00:00:00Z";
const HOUR = 3600;

const device = (entityId: string, on: number, unknown: number): Usage["devices"][number] => ({
  device_kind: "switch",
  entity_id: entityId,
  on_seconds: on,
  unknown_seconds: unknown,
  switch_ons: 1,
  days: [{ date: "2026-10-11", on_seconds: on, unknown_seconds: unknown }],
  hours: Array.from({ length: 24 }, () => 0),
  unknown_intervals: [{ start: START, end: END, reason: "unavailable" }],
  periods: [{ start: START, end: END, state: "off" }],
});

const usage: Usage = {
  start: START,
  end: END,
  timezone: "Europe/Copenhagen",
  devices: [device(DESK, 2 * HOUR, HOUR), device(PLUG, 4 * HOUR, HOUR)],
};

const panel = () =>
  screen.queryByRole("region", { name: "Agent" }) ??
  screen.getByRole("complementary", { name: "Agent" });

/** The card the panel draws for one show_chart call. */
async function drawnCard(spec: Record<string, unknown>, names: ReadonlyMap<string, string>) {
  installFakeApi({ "GET /api/usage": usage });
  installFakeAegra(
    createFakeAegra({
      onRun: ({ question }) =>
        question
          ? [
              { tool: { name: "show_chart", args: { spec } }, result: "Chart shown." },
              { say: "Here it is." },
            ]
          : [],
    }),
  );
  const user = userEvent.setup();
  render(<AgentPanel names={names} onClose={() => {}} />);
  await user.type(await waitFor(() => within(panel()).getByRole("textbox")), "Chart it{Enter}");
  await within(panel()).findByText("Here it is.");
  return waitFor(
    () => {
      const surface = panel().querySelector("svg.recharts-surface");
      const card = surface?.closest("figure");
      if (!card) throw new Error("No chart card yet");
      return card;
    },
    { timeout: 5000 },
  );
}

// jsdom's SVG elements have no specific classes; these match the element names the
// exporter tests for. Shapes other than paths are left out: jsdom has no geometry for them.
const SVG_CLASSES: Record<string, (element: Element) => boolean> = {
  SVGGraphicsElement: () => true,
  SVGGElement: (element) => element.localName === "g",
  SVGTextContentElement: (element) => ["text", "tspan"].includes(element.localName),
  SVGTextElement: (element) => element.localName === "text",
  SVGPathElement: (element) => element.localName === "path",
  SVGRectElement: () => false,
  SVGLineElement: () => false,
  SVGCircleElement: () => false,
  SVGPolylineElement: () => false,
  SVGPolygonElement: () => false,
};

/** The text the exporter paints on its canvas, in paint order. */
async function paintedText(card: HTMLElement): Promise<string> {
  const texts: string[] = [];
  const state: Record<string | symbol, unknown> = {
    font: "12px sans-serif",
    fillText: (text: string) => texts.push(text),
    measureText: (text: string) => ({ width: text.length * 6 }),
  };
  // Every other drawing call does nothing; properties such as fillStyle just store.
  const pen = new Proxy(state, {
    get: (target, key) => (key in target ? target[key] : () => {}),
    set: (target, key, value) => Reflect.set(target, key, value),
  });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    pen as unknown as CanvasRenderingContext2D,
  );
  vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((done) =>
    done(new Blob(["png"], { type: "image/png" })),
  );
  for (const [name, matches] of Object.entries(SVG_CLASSES)) {
    // `instanceof` asks the right-hand object's Symbol.hasInstance.
    vi.stubGlobal(name, {
      [Symbol.hasInstance]: (element: unknown) => element instanceof SVGElement && matches(element),
    });
  }
  vi.stubGlobal(
    "DOMMatrix",
    class {
      multiply() {
        return this;
      }
      scale() {
        return this;
      }
      translate() {
        return this;
      }
    },
  );
  vi.stubGlobal("Path2D", class {});
  const svg: Record<string, unknown> = {
    getCTM: () => null,
    getNumberOfChars(this: Element) {
      return this.textContent?.length ?? 0;
    },
    getStartPositionOfChar: () => ({ x: 0, y: 0 }),
  };
  for (const [name, value] of Object.entries(svg)) {
    Object.defineProperty(SVGElement.prototype, name, { configurable: true, value });
  }
  try {
    expect(await chartPng(card)).not.toBeNull();
  } finally {
    for (const name of Object.keys(svg)) Reflect.deleteProperty(SVGElement.prototype, name);
  }
  return texts.join(" ");
}

beforeEach(() => {
  localStorage.clear();
  const getBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function boundingClientRect(this: HTMLElement) {
      return this.classList.contains("recharts-responsive-container")
        ? DOMRect.fromRect({ width: 480, height: 224 })
        : getBoundingClientRect.call(this);
    },
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

it("paints a bar chart's title, categories and series names as literal text", async () => {
  const card = await drawnCard(
    {
      kind: "bar",
      title: "Switch-ons",
      categories: ["Sunday"],
      series: [{ name: "<b>x</b>", values: [2] }],
    },
    new Map(),
  );

  const text = await paintedText(card);

  expect(text).toContain("Switch-ons");
  expect(text).toContain("Sunday");
  expect(text).toContain("<b>x</b>");
});

it("names every device of a usage chart in its PNG, as literal text", async () => {
  const names = new Map([
    [DESK, "<b>x</b>"],
    [PLUG, "Kettle"],
  ]);
  const card = await drawnCard(
    { kind: "usage_by_day", entity_ids: [DESK, PLUG], start: START, end: END },
    names,
  );

  const text = await paintedText(card);

  expect(text).toContain("<b>x</b>");
  expect(text).toContain("Kettle");
});
