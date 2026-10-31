import { render, screen } from "@testing-library/react";
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
