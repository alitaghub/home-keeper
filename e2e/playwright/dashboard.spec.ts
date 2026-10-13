import { expect, test } from "@playwright/test";

// The demo catalog's untrusted entity (apps/observer/src/home_observer/demo.py).
const SCRIPT_ENTITY_ID = "light.hue_reading";
const SCRIPT_NAME = "Reading light <script>alert('untrusted')</script>";
const LAMP = "Reading lamp";

// A collection pass runs every ~10 s, so the first baseline can take a few passes.
const COLLECTION = { timeout: 45_000 };

// A reused Compose volume can still hold the selection from an earlier run.
test.beforeEach(async ({ request }) => {
  const reset = await request.put(`/api/selection/${SCRIPT_ENTITY_ID}`, {
    headers: { "X-Home-Observer": "1" },
    data: { selected: false },
  });
  expect(reset.ok()).toBe(true);
});

test("monitors a demo lamp and keeps its untrusted name inert", async ({ page, request }) => {
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => {
    dialogs.push(dialog.message());
    void dialog.dismiss();
  });

  const catalog = await request.get("/api/catalog");
  expect(catalog.ok()).toBe(true);
  expect(await catalog.json()).toMatchObject({
    entities: expect.arrayContaining([
      expect.objectContaining({ id: SCRIPT_ENTITY_ID, name: SCRIPT_NAME }),
    ]),
  });

  await page.goto("/");
  await expect(page.locator("#source")).toHaveText("SYNTHETIC DEMO");
  await expect(page.locator("#source-notice")).toHaveText(
    "Demo data only. No hardware, Home Assistant, model, or device controls are connected.",
  );

  const lamps = page.locator("#catalog");
  await lamps.getByRole("button", { name: `Monitor ${LAMP}` }).click();
  await expect(lamps.getByRole("button", { name: `Stop monitoring ${LAMP}` })).toBeVisible();

  const card = page
    .locator("#monitored article")
    .filter({ has: page.getByRole("heading", { name: LAMP }) });
  await expect(card).toBeVisible();
  await expect(card.getByText(/^(on|off|unavailable|unknown) · /)).toBeVisible(COLLECTION);
  await expect(card.getByText(/^Source: demo · Evidence ID: /)).toBeVisible();

  // The name containing a script tag must never become markup.
  await expect(page.locator("body script")).toHaveCount(0);
  expect(dialogs).toEqual([]);

  await card.getByRole("button", { name: `Stop monitoring ${LAMP}` }).click();
  await expect(lamps.getByRole("button", { name: `Monitor ${LAMP}` })).toBeVisible();
  await expect(card).toHaveCount(0);
});
