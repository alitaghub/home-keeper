import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import * as fixtures from "../test/fixtures";
import { Catalog } from "./Catalog";

const { desk, reading, smartPlug, roomGroup, ungroupedSensor } = fixtures;

function renderCatalog() {
  render(
    <Catalog
      entities={[desk, reading, smartPlug, roomGroup, ungroupedSensor]}
      homeAssistantUrl={null}
      selected={[]}
      onToggle={() => {}}
    />,
  );
  return userEvent.setup();
}

const monitorButton = (name: string) => screen.queryByRole("button", { name: `Monitor ${name}` });

/** The collapsed group of unsupported entities. */
function otherEntities() {
  const group = screen.getByText(/other entities/i).closest("details");
  if (!group) throw new Error("Unsupported entities are not in a collapsed group");
  return group;
}

describe("catalog on the Setup tab", () => {
  it("groups supported entities by kind and keeps unsupported ones collapsed in a last group", () => {
    renderCatalog();

    const lights = screen.getByRole("heading", { name: "Lights" });
    const plugs = screen.getByRole("heading", { name: "Plugs and switches" });
    const other = otherEntities();
    const follows = (before: Node, after: Node) =>
      Boolean(before.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(follows(lights, plugs)).toBe(true);
    expect(follows(plugs, other)).toBe(true);
    expect(follows(screen.getByRole("button", { name: "Monitor Desk lamp" }), plugs)).toBe(true);
    expect(follows(plugs, screen.getByRole("button", { name: "Monitor Smart plug" }))).toBe(true);
    expect(other).not.toHaveAttribute("open");
    expect(other).toHaveTextContent("All living room lights");
    expect(other).toHaveTextContent("Ungrouped sensor");
    expect(other).toHaveTextContent("Only lights, plugs and switches are supported");
  });

  it("filters by device name or entity name, ignoring case, and restores the list when cleared", async () => {
    const user = renderCatalog();
    const search = screen.getByRole("searchbox", { name: "Search devices" });

    // A device name matches, and so does an entity name that differs from it.
    await user.type(search, "DESK LAMP");
    expect(monitorButton("Desk lamp")).toBeInTheDocument();
    expect(monitorButton("Reading lamp")).toBeNull();
    expect(monitorButton("Smart plug")).toBeNull();

    // A group the search leaves empty is hidden.
    await user.clear(search);
    await user.type(search, "smart plug");
    expect(screen.getByRole("heading", { name: "Plugs and switches" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Lights" })).toBeNull();

    await user.clear(search);
    await user.type(search, "reading light");
    expect(monitorButton("Reading lamp")).toBeInTheDocument();
    expect(monitorButton("Desk lamp")).toBeNull();

    // Unsupported entities are filtered too.
    await user.clear(search);
    await user.type(search, "ungrouped");
    expect(otherEntities()).toHaveTextContent("Ungrouped sensor");
    expect(otherEntities()).not.toHaveTextContent("All living room lights");
    expect(monitorButton("Desk lamp")).toBeNull();

    await user.clear(search);
    expect(screen.getByRole("heading", { name: "Lights" })).toBeInTheDocument();
    for (const name of ["Desk lamp", "Reading lamp", "Smart plug"]) {
      expect(monitorButton(name)).toBeInTheDocument();
    }
    expect(otherEntities()).toHaveTextContent("All living room lights");
    expect(otherEntities()).toHaveTextContent("Ungrouped sensor");
  });

  it("says when a search matches only unsupported entities, and when it matches nothing", async () => {
    const user = renderCatalog();
    const search = screen.getByRole("searchbox", { name: "Search devices" });

    await user.type(search, "living room");
    expect(screen.getByText(/No supported devices match “living room”/)).toBeInTheDocument();
    expect(screen.queryByText(/^No devices match/)).toBeNull();

    await user.clear(search);
    await user.type(search, "zzz");
    expect(screen.getByText(/^No devices match “zzz”/)).toBeInTheDocument();
    expect(screen.queryByText(/No supported devices match/)).toBeNull();
  });

  it("still says what a search found when the home has no supported devices", async () => {
    render(
      <Catalog
        entities={[ungroupedSensor]}
        homeAssistantUrl={null}
        selected={[]}
        onToggle={() => {}}
      />,
    );
    const user = userEvent.setup();
    const search = screen.getByRole("searchbox", { name: "Search devices" });

    await user.type(search, "sensor");
    expect(screen.getByText(/No supported devices match “sensor”/)).toBeInTheDocument();
    expect(screen.queryByText(/^No devices match/)).toBeNull();

    await user.clear(search);
    await user.type(search, "zzz");
    expect(screen.getByText(/^No devices match “zzz”/)).toBeInTheDocument();
    expect(screen.queryByText(/No supported devices match/)).toBeNull();
  });

  it("tells apart two entities of one device by name in the card and the button", () => {
    const hall = { ...fixtures.thermostat, device_id: "hall-sensor", device_name: "Hall sensor" };
    render(
      <Catalog
        entities={[
          { ...hall, id: "sensor.hall_temperature", name: "Temperature" },
          { ...hall, id: "sensor.hall_humidity", name: "Humidity" },
        ]}
        homeAssistantUrl={null}
        selected={[]}
        onToggle={() => {}}
      />,
    );

    expect(screen.getByText("Hall sensor · Temperature")).toBeInTheDocument();
    expect(screen.getByText("Hall sensor · Humidity")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Monitor Hall sensor · Temperature" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Monitor Hall sensor · Humidity" }),
    ).toBeInTheDocument();
  });
});
