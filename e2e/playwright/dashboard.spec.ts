import { expect, test } from "@playwright/test";
import type { components } from "../../apps/web/src/api/schema";

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

  // Choosing lights lives on the Setup tab.
  await page.goto("/#setup");
  await expect(page.locator("#source")).toHaveText("SYNTHETIC DEMO");
  // The e2e demo stack runs with the control profile, so the notice says light control is on.
  await expect(page.locator("#source-notice")).toContainText(
    "Light control is on for monitored lights.",
  );
  await expect(page.locator("#source-notice")).not.toContainText("device controls are connected");

  const lamps = page.locator("#catalog");
  await lamps.getByRole("button", { name: `Monitor ${LAMP}` }).click();
  await expect(lamps.getByRole("button", { name: `Stop monitoring ${LAMP}` })).toBeVisible();

  await page.getByRole("tab", { name: "Home" }).click();
  const card = page
    .locator("#monitored article")
    .filter({ has: page.getByRole("heading", { name: LAMP }) });
  await expect(card).toBeVisible();
  const state = card.locator(".tile-state");
  await expect(state).toHaveText(/^(On|Off|Unavailable|Unknown)/, COLLECTION);
  // The evidence sits behind the card's Details.
  await card.getByText("Details").click();
  await expect(card.getByText(/^Source: demo · Evidence ID: /)).toBeVisible();

  // Control is on, so a light with a recorded on/off state offers a toggle. The demo
  // replay can change the light between the read and the click; the API then refuses
  // with already_in_state or unavailable, so read the toggle again and retry.
  const toggle = card.getByRole("button", { name: new RegExp(`^Turn (on|off) ${LAMP}$`) });
  let now: string | undefined;
  for (let attempt = 0; attempt < 5 && now === undefined; attempt += 1) {
    await expect(toggle).toBeVisible(COLLECTION);
    const action = (await toggle.getAttribute("aria-label"))?.match(/^Turn (on|off)/)?.[1];
    expect(action).toBeDefined();
    const [response] = await Promise.all([
      page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/command")),
      toggle.click(),
    ]);
    if (response.status() === 409) {
      const refusal: components["schemas"]["CommandRefused"] = await response.json();
      expect(["already_in_state", "unavailable"]).toContain(refusal.detail.code);
      continue;
    }
    expect(response.status()).toBe(200);
    now = action;
  }
  expect(now, "every command attempt met a replay change").toBeDefined();
  await expect(page.locator("#message")).toHaveText(`${LAMP} is now ${now}.`, COLLECTION);
  // The toggle shows the recorded state, so the card flips only once the change is recorded.
  await expect(state).toHaveText(new RegExp(`^${now === "on" ? "On" : "Off"}`), COLLECTION);

  // The name containing a script tag must never become markup.
  await expect(page.locator("body script")).toHaveCount(0);
  expect(dialogs).toEqual([]);

  await card.getByRole("button", { name: `Stop monitoring ${LAMP}` }).click();
  await expect(card).toHaveCount(0);
  await page.getByRole("tab", { name: "Setup" }).click();
  await expect(lamps.getByRole("button", { name: `Monitor ${LAMP}` })).toBeVisible();
});
