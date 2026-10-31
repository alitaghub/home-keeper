import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../api/client";
import * as fixtures from "../test/fixtures";
import { Monitored } from "./Monitored";

const ignore = () => {};

/** The one status indicator on the card named `name`. */
function statusOf(name: string) {
  const card = screen.getByRole("heading", { name }).closest("article");
  if (!card) throw new Error(`${name} is not inside a card`);
  const markers = card.querySelectorAll("[data-status]");
  expect(markers).toHaveLength(1);
  return markers[0];
}

describe("device status on Home", () => {
  const { desk, smartPlug } = fixtures;
  // Off and unknown read alike as plain text; the marker keeps all three apart.
  it.each<[string, "on" | "off" | "unknown", string, Schemas["MonitoredEntity"]]>([
    ["a lamp that is on", "on", "On · 100%", fixtures.monitoredLight(desk, "on", "available", 255)],
    ["a lamp that is off", "off", "Off", fixtures.monitoredLight(desk, "off")],
    ["a plug that is on", "on", "On", fixtures.monitoredSwitch(smartPlug, "on")],
    ["a plug that is off", "off", "Off", fixtures.monitoredSwitch(smartPlug, "off")],
    [
      "an unavailable lamp",
      "unknown",
      "Unavailable",
      fixtures.monitoredLight(desk, "off", "unavailable"),
    ],
    ["a lamp in an unknown state", "unknown", "Unknown", fixtures.monitoredLight(desk, "unknown")],
    [
      "a lamp Home Assistant cannot be read for",
      "unknown",
      "State unknown",
      fixtures.unknownDevice(desk),
    ],
  ])("marks %s as %s", (_, status, text, entity) => {
    render(
      <Monitored
        entities={[entity]}
        usage={null}
        onStop={ignore}
        onSwitch={null}
        onHistory={ignore}
        onChooseLights={ignore}
        sending={new Set()}
      />,
    );

    const marker = statusOf(entity.device_name || entity.name);
    expect(marker).toHaveAttribute("data-status", status);
    expect(marker).toHaveTextContent(text);
  });
});

function show(entities: Schemas["MonitoredEntity"][], onSwitch: (() => void) | null = null) {
  return render(
    <Monitored
      entities={entities}
      usage={null}
      onStop={ignore}
      onSwitch={onSwitch}
      onHistory={ignore}
      onChooseLights={ignore}
      sending={new Set()}
    />,
  );
}

const headings = () =>
  screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);

describe("sections on Home", () => {
  const { desk, smartPlug, thermostat } = fixtures;

  it("shows a generic device under Other with Home Assistant's word for its state", () => {
    show([fixtures.monitoredState(thermostat, "heat", "Heat")], () => {});

    const other = screen.getByRole("heading", { level: 2, name: "Other" }).closest("section");
    if (!other) throw new Error("Other is not a section");
    expect(within(other).getByRole("heading", { name: "Hall thermostat" })).toBeInTheDocument();
    expect(within(other).getAllByText("Heat").length).toBeGreaterThan(0);
    expect(within(other).getByText(/^Changed /)).toBeInTheDocument();
    expect(other.querySelector("#monitored-other")).toContainElement(
      within(other).getByRole("heading", { name: "Hall thermostat" }),
    );
  });

  it("gives a generic device no dimmer, no switch buttons and no usage history, even with control on", () => {
    show([fixtures.monitoredState(thermostat, "on", "Detected")], () => {});

    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Turn (on|off)/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Usage history/ })).not.toBeInTheDocument();
    // It can still be stopped.
    expect(
      screen.getByRole("button", { name: "Stop monitoring Hall thermostat" }),
    ).toBeInTheDocument();
  });

  it("orders the sections Lights, Plugs and switches, Other", () => {
    show([
      fixtures.monitoredState(thermostat, "heat", "Heat"),
      fixtures.monitoredSwitch(smartPlug, "on"),
      fixtures.monitoredLight(desk, "on"),
    ]);

    expect(headings()).toEqual(["Lights", "Plugs and switches", "Other"]);
  });

  it("shows no Other heading when no generic device is monitored", () => {
    show([fixtures.monitoredLight(desk, "on"), fixtures.monitoredSwitch(smartPlug, "off")]);

    expect(headings()).toEqual(["Lights", "Plugs and switches"]);
  });

  it("shows no Lights heading when only a generic device is monitored", () => {
    show([fixtures.monitoredState(thermostat, "heat", "Heat")]);

    expect(headings()).toEqual(["Other"]);
  });

  it("shows a state and label shaped like markup as literal text", () => {
    const markup = "<img src=x onerror=alert(1)>";
    const { container } = show([fixtures.monitoredState(thermostat, "<b>heat</b>", markup)]);

    expect(container.querySelector("img, b")).toBeNull();
    expect(screen.getAllByText(markup).length).toBeGreaterThan(0);
  });
});
