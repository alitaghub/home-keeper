import { expect, test } from "@playwright/test";
import type { components } from "../../apps/web/src/api/schema";

// The demo catalog's untrusted entity (apps/keeper/src/home_keeper/demo.py).
const SCRIPT_ENTITY_ID = "light.hue_reading";
const SCRIPT_NAME = "Reading light <script>alert('untrusted')</script>";
const LAMP = "Reading lamp";

// The demo reader answers at once; the margin covers a slow first start of the stack.
const COLLECTION = { timeout: 45_000 };
const RECORDER_DOC =
  "https://github.com/alitaghub/home-keeper/blob/main/docs/home-assistant.md#history-retention";

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
  await expect(page.locator("#source-notice")).not.toContainText("Light control is off");

  const lamps = page.locator("#catalog");
  await lamps.getByRole("button", { name: `Monitor ${LAMP}` }).click();
  await expect(lamps.getByRole("button", { name: `Stop monitoring ${LAMP}` })).toBeVisible();

  await page.getByRole("tab", { name: "Home" }).click();
  const card = page
    .locator("#monitored article")
    .filter({ has: page.getByRole("heading", { name: LAMP }) });
  await expect(card).toBeVisible();
  // The card's one status indicator, which holds the state text.
  const state = card.locator("[data-status]");
  await expect(state).toHaveText(/^(On|Off|Unavailable|Unknown)/, COLLECTION);
  // The card shows what the demo home reports now; it names no recorded evidence.
  await card.getByText("Details").click();
  await expect(card.getByText(/Evidence ID/)).toHaveCount(0);

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
  // The toggle shows the reported state, so the card flips once Home Assistant reports it.
  await expect(state).toHaveText(new RegExp(`^${now === "on" ? "On" : "Off"}`), COLLECTION);

  // The card draws its last day from /api/usage; its usage history opens in a dialog.
  await expect(card.getByRole("img", { name: /^Last 24 hours: on / })).toBeVisible(COLLECTION);
  await card.getByRole("button", { name: `Usage history of ${LAMP}` }).click();
  const history = page.getByRole("dialog", { name: LAMP });
  await expect(history.getByText("Switched on")).toBeVisible();
  await history.getByRole("button", { name: "7 days" }).click();
  await expect(history.getByRole("heading", { name: "On per day" })).toBeVisible();
  await history.getByRole("button", { name: "Close" }).click();
  await expect(history).toHaveCount(0);

  // The demo has a day of history from its first minute.
  const anHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
  const older = await request.get("/api/events", {
    params: { entity_id: SCRIPT_ENTITY_ID, end: anHourAgo },
  });
  expect(older.ok()).toBe(true);
  const olderPage: components["schemas"]["EventsPage"] = await older.json();
  expect(olderPage.events.length).toBeGreaterThan(0);

  // History lists the lamp's changes in words at Home Assistant's time, including the command.
  await page.getByRole("tab", { name: "History" }).click();
  const rows = page.locator("#timeline article").filter({ hasText: LAMP });
  await expect(rows.filter({ hasText: `turned ${now}` }).first()).toBeVisible(COLLECTION);
  await expect(rows.first().locator("time")).toHaveAttribute("datetime", /^\d{4}-\d{2}-\d{2}T/);
  // The panel the "Recorded changes" heading names.
  const changes = page.getByRole("region", { name: "Recorded changes" });
  await expect(changes).not.toContainText(
    /Evidence ID|Retained since|Retention cutoff|received|Stored/i,
  );
  await expect(changes.getByRole("link", { name: "recorder settings" })).toHaveAttribute(
    "href",
    RECORDER_DOC,
  );

  // The name containing a script tag must never become markup.
  await expect(page.locator("body script")).toHaveCount(0);
  expect(dialogs).toEqual([]);

  await page.getByRole("tab", { name: "Home" }).click();
  await card.getByRole("button", { name: `Stop monitoring ${LAMP}` }).click();
  await expect(card).toHaveCount(0);
  await page.getByRole("tab", { name: "Setup" }).click();
  await expect(lamps.getByRole("button", { name: `Monitor ${LAMP}` })).toBeVisible();
});
