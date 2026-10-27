import { expect, test } from "@playwright/test";

// The demo catalog's untrusted entity (apps/keeper/src/home_keeper/demo.py).
const SCRIPT_ENTITY_ID = "light.hue_reading";
const SCRIPT_NAME = "Reading light <script>alert('untrusted')</script>";
const LAMP = "Reading lamp";

// A collection pass runs every ~10 s, so the first baseline can take a few passes.
const COLLECTION = { timeout: 45_000 };

// A reused Compose volume can still hold the selection from an earlier run.
test.beforeEach(async ({ request }) => {
  const reset = await request.put(`/api/selection/${SCRIPT_ENTITY_ID}`, {
    headers: { "X-Home-Keeper": "1" },
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
  // The e2e demo stack runs with the control profile, so the notice says light control is on.
  await expect(page.locator("#source-notice")).toContainText(
    "Light control is on for monitored lights.",
  );
  await expect(page.locator("#source-notice")).not.toContainText("device controls are connected");

  const lamps = page.locator("#catalog");
  await lamps.getByRole("button", { name: `Monitor ${LAMP}` }).click();
  await expect(lamps.getByRole("button", { name: `Stop monitoring ${LAMP}` })).toBeVisible();

  const card = page
    .locator("#monitored article")
    .filter({ has: page.getByRole("heading", { name: LAMP }) });
  await expect(card).toBeVisible();
  await expect(card.getByText(/^(on|off|unavailable|unknown) · /)).toBeVisible(COLLECTION);
  await expect(card.getByText(/^Source: demo · Evidence ID: /)).toBeVisible();

  // Control is on, so a light with a recorded on/off state offers a toggle.
  const recorded = card.getByText(/^(on|off) · /);
  await expect(recorded).toBeVisible(COLLECTION);
  const wasOn = (await recorded.textContent())?.startsWith("on") ?? false;
  const toggle = card.getByRole("button", { name: `Turn ${wasOn ? "off" : "on"} ${LAMP}` });
  await toggle.click();
  const now = wasOn ? "off" : "on";
  await expect(page.locator("#message")).toHaveText(`${LAMP} is now ${now}.`, COLLECTION);
  // The toggle shows the recorded state, so the card flips only once the change is recorded.
  await expect(card.getByText(new RegExp(`^${now} · `))).toBeVisible(COLLECTION);

  // The name containing a script tag must never become markup.
  await expect(page.locator("body script")).toHaveCount(0);
  expect(dialogs).toEqual([]);

  await card.getByRole("button", { name: `Stop monitoring ${LAMP}` }).click();
  await expect(lamps.getByRole("button", { name: `Monitor ${LAMP}` })).toBeVisible();
  await expect(card).toHaveCount(0);
});
