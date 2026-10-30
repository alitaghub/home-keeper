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
