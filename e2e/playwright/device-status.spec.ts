import { expect, type Page, test } from "@playwright/test";
import * as fixtures from "../../apps/web/src/test/fixtures";

const SCHEMES = ["light", "dark"] as const;
const STATUSES = ["off", "on", "unknown"];

// Fixed cards, so the test does not wait for the demo replay to pass through each state.
const MONITORED = {
  entities: [
    fixtures.monitoredLight(fixtures.desk, "on", "available", 255),
    fixtures.monitoredLight(fixtures.reading, "off"),
    fixtures.monitoredSwitch(fixtures.smartPlug, "off", "unavailable"),
  ],
};

/** Each visible status indicator's colors, as the browser computes them. */
async function statusLooks(page: Page) {
  const indicators = page.locator("#monitored [data-status], #monitored-switches [data-status]");
  await expect(indicators).toHaveCount(STATUSES.length);
  for (const indicator of await indicators.all()) await expect(indicator).toBeVisible();
  return indicators.evaluateAll((nodes) =>
    nodes.map((node) => {
      const style = getComputedStyle(node);
      return [
        node.getAttribute("data-status") ?? "",
        [style.color, style.backgroundColor, style.borderColor].join(" | "),
      ] as const;
    }),
  );
}

test("on, off and unknown devices each look different in light and dark", async ({ page }) => {
  await page.route("**/api/monitored", (route) => route.fulfill({ json: MONITORED }));

  const backgrounds = new Set<string>();
  for (const colorScheme of SCHEMES) {
    // Set before loading, so no color transition is caught halfway.
    await page.emulateMedia({ colorScheme });
    await page.goto("/");
    const shown = Object.fromEntries(await statusLooks(page));
    expect(Object.keys(shown).sort()).toEqual(STATUSES);
    expect(new Set(Object.values(shown)).size, `${colorScheme}: ${JSON.stringify(shown)}`).toBe(
      STATUSES.length,
    );
    backgrounds.add(
      await page.locator("body").evaluate((body) => getComputedStyle(body).backgroundColor),
    );
  }
  // The page follows the system setting.
  expect(backgrounds.size).toBe(SCHEMES.length);
});

/** The usage strip's fills, as the browser computes them: track, on, off and the unavailable hatch. */
async function stripFills(page: Page) {
  const strip = page.locator("#monitored svg[role=img]", {
    has: page.locator('[data-segment="unavailable"]'),
  });
  await expect(strip).toBeVisible();
  return strip.evaluate((svg) => {
    const fill = (selector: string) => {
      const node = svg.querySelector(selector);
      return node ? getComputedStyle(node).fill : "missing";
    };
    return {
      track: fill(":scope > rect:not([data-segment])"),
      on: fill('[data-segment="on"]'),
      off: fill('[data-segment="off"]'),
      hatch: fill("pattern rect"),
    };
  });
}

test("the usage strip's track, on, off and unavailable spans each look different in light and dark", async ({
  page,
}) => {
  await page.route("**/api/monitored", (route) => route.fulfill({ json: MONITORED }));
  await page.route("**/api/usage**", (route) => route.fulfill({ json: fixtures.usage }));

  for (const colorScheme of SCHEMES) {
    await page.emulateMedia({ colorScheme });
    await page.goto("/");
    const fills = await stripFills(page);
    expect(Object.values(fills), `${colorScheme}: ${JSON.stringify(fills)}`).not.toContain(
      "missing",
    );
    expect(new Set(Object.values(fills)).size, `${colorScheme}: ${JSON.stringify(fills)}`).toBe(4);
  }
});

test("a monitored door sensor shows under Other on/off with a strip and no command buttons", async ({
  page,
}) => {
  const door = fixtures.monitoredOnOff(fixtures.frontDoor, "on", "Open");
  await page.route("**/api/monitored", (route) => route.fulfill({ json: { entities: [door] } }));
  await page.route("**/api/usage**", (route) => route.fulfill({ json: fixtures.doorUsage }));
  // Control on, so a light or plug would show its switch.
  await page.route("**/api/control", (route) => route.fulfill({ json: fixtures.controlOn }));

  await page.goto("/");

  const section = page.locator("section", {
    has: page.getByRole("heading", { level: 2, name: "Other on/off" }),
  });
  const card = section.locator("#monitored-onoff article", { hasText: "Front door" });
  await expect(card).toBeVisible();
  await expect(card.getByText("Open").first()).toBeVisible();
  await expect(card.getByRole("img", { name: /^Last 24 hours/ })).toBeVisible();
  await expect(card.getByRole("button", { name: "Usage history of Front door" })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Turn (on|off)/ })).toHaveCount(0);
  await expect(page.getByRole("slider")).toHaveCount(0);
  // It is not a light or a plug.
  await expect(page.locator("#monitored article")).toHaveCount(0);
});
