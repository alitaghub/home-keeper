// The agent panel against the real agent graph in Aegra, with a deterministic model
// (e2e/dashboard_graph.py), behind the dashboard's own address. Started by
// e2e/compose_agent_panel_proof.py; never against a real home.
import { execFileSync } from "node:child_process";
import { expect, type Locator, type Page, test } from "@playwright/test";
import type { paths } from "../../apps/web/src/api/schema";

const DESK = "light.hue_desk";
const DESK_NAME = "Desk lamp";
const WRITE = { "X-Home-Keeper": "1" };
// The model answers at once; the margin covers a slow first run of the graph.
const ANSWER = { timeout: 30_000 };
// The dashboard polls every 3 seconds.
const POLL = { timeout: 15_000 };

test.describe.configure({ mode: "serial" });

const agentPanel = (page: Page) =>
  page
    .getByRole("region", { name: "Agent" })
    .or(page.getByRole("complementary", { name: "Agent" }));

async function openPanel(page: Page): Promise<Locator> {
  const panel = agentPanel(page);
  // The panel restores open after a reload but loads lazily, so read the button's state.
  const button = page.getByRole("button", { name: "Ask" });
  if ((await button.getAttribute("aria-expanded")) !== "true") await button.click();
  await expect(panel).toBeVisible();
  return panel;
}

async function ask(panel: Locator, question: string) {
  const input = panel.getByRole("textbox");
  await input.fill(question);
  await input.press("Enter");
}

const deskCard = (page: Page) =>
  page
    .locator("#monitored article")
    .filter({ has: page.getByRole("heading", { name: DESK_NAME }) });

type Monitored = paths["/api/monitored"]["get"]["responses"][200]["content"]["application/json"];

async function deskState(page: Page): Promise<string | undefined> {
  const response = await page.request.get("/api/monitored");
  expect(response.ok()).toBe(true);
  const body: Monitored = await response.json();
  return body.entities.find((entity) => entity.id === DESK)?.state?.state;
}

/** Home's content sits right below the header: short content must not stretch the rows. */
async function expectCompactHome(page: Page) {
  const header = await page.getByRole("banner").boundingBox();
  const home = await page.locator("#panel-home").boundingBox();
  const message = await page.locator("#message").boundingBox();
  expect(header && home && message).toBeTruthy();
  if (header && home && message) {
    expect(message.height).toBeLessThanOrEqual(40);
    expect(home.y - (header.y + header.height)).toBeLessThanOrEqual(80);
  }
}

// A reused Compose volume can still hold the selection from an earlier run.
test.beforeAll(async ({ request }) => {
  const reset = await request.put(`/api/selection/${DESK}`, {
    headers: WRITE,
    data: { selected: false },
  });
  expect(reset.ok()).toBe(true);
});

test("opens beside the cards from Ask and loads its code only then", async ({ page }) => {
  const scripts: string[] = [];
  page.on("request", (request) => {
    if (request.resourceType() === "script") scripts.push(request.url());
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await expect(page.locator("#source")).toHaveText("SYNTHETIC DEMO");
  await expect(page.locator("#status-line")).toContainText("Home Assistant");
  await expect(agentPanel(page)).toHaveCount(0);
  const firstLoad = scripts.length;
  // Where the Home content sits with the panel closed.
  const layout = async () => ({
    home: (await page.locator("#panel-home").boundingBox())?.y,
    status: (await page.locator("#status-line").boundingBox())?.y,
    message: (await page.locator("#message").boundingBox())?.height,
  });
  const closed = await layout();
  await expectCompactHome(page);

  const panel = await openPanel(page);

  await expect.poll(() => scripts.length).toBeGreaterThan(firstLoad);
  await expect(panel.getByRole("textbox")).toBeVisible();
  // The full-height panel must not stretch the page's rows and push the cards down.
  const open = await layout();
  expect(Math.abs((open.home ?? Infinity) - (closed.home ?? 0))).toBeLessThanOrEqual(4);
  expect(Math.abs((open.status ?? Infinity) - (closed.status ?? 0))).toBeLessThanOrEqual(4);
  expect(open.message).toBe(closed.message);
  await expectCompactHome(page);
  const main = await page.locator("main").boundingBox();
  const box = await panel.boundingBox();
  expect(main && box).toBeTruthy();
  if (main && box) {
    expect(box.width).toBeGreaterThan(300);
    expect(box.width).toBeLessThan(480);
    // Beside the cards, never over them.
    expect(box.x >= main.x + main.width || box.x + box.width <= main.x).toBe(true);
  }
});

test("keeps Home's content right below the header on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/");
  await expect(page.locator("#status-line")).toContainText("Home Assistant");
  await expect(agentPanel(page)).toHaveCount(0);

  await expectCompactHome(page);
});

test("is a full-screen sheet on a phone that closes back to the tab", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/#history");
  const panel = await openPanel(page);
  const box = await panel.boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(370);

  await page.getByRole("button", { name: "Close agent panel" }).click();

  await expect(panel).toBeHidden();
  await expect(page.getByRole("tab", { name: "History", selected: true })).toBeVisible();
  await expect(page.locator("#panel-history")).toBeVisible();
});

test("streams an answer to a question once", async ({ page }) => {
  await page.goto("/");
  const panel = await openPanel(page);

  await ask(panel, "hello there");

  await expect(panel.getByText("Reply: hello there")).toBeVisible(ANSWER);
  // The model's reply streams under two ids; give a second copy time to appear.
  await page.waitForTimeout(1000);
  await expect(panel.getByText("Reply: hello there")).toHaveCount(1);

  // A reload keeps the conversation.
  await page.reload();
  const reloaded = await openPanel(page);
  await expect(reloaded.getByText("Reply: hello there")).toBeVisible(ANSWER);
  await expect(reloaded.getByText("Reply: hello there")).toHaveCount(1);
});

test("shows the first half of an answer while the model is still writing it", async ({ page }) => {
  await page.goto("/");
  const panel = await openPanel(page);
  await panel.getByRole("button", { name: "New chat" }).click();

  // "slow" makes the model wait between its two chunks (e2e/dashboard_graph.py).
  await ask(panel, "slow hello there");

  await expect(panel).toContainText("Reply: slow", ANSWER);
  await expect(panel).not.toContainText("Reply: slow hello there");
  await expect(panel).toContainText("Reply: slow hello there", ANSWER);
});

test("monitors a device the agent proposes once you approve it", async ({ page }) => {
  await page.goto("/");
  const panel = await openPanel(page);
  await panel.getByRole("button", { name: "New chat" }).click();

  await ask(panel, `monitor ${DESK}`);
  await panel.getByRole("button", { name: `Approve: Start monitoring ${DESK_NAME}` }).click(ANSWER);

  // The monitor tool quotes the entity id.
  await expect(panel.getByText(`Result: Now monitoring "${DESK}".`, { exact: false })).toBeVisible(
    ANSWER,
  );
  await expect(panel.getByRole("button", { name: /^(Approve|Reject): / })).toHaveCount(0);
  // The card arrives through the dashboard's own poll.
  await expect(deskCard(page)).toBeVisible(POLL);
});

test("changes nothing when you reject a proposal, and says so", async ({ page }) => {
  await page.goto("/");
  await expect(deskCard(page)).toBeVisible(POLL);
  const panel = await openPanel(page);
  await panel.getByRole("button", { name: "New chat" }).click();

  await ask(panel, `stop ${DESK}`);
  await panel.getByRole("button", { name: `Reject: Stop monitoring ${DESK_NAME}` }).click(ANSWER);

  await expect(panel.getByText(/Result: User rejected the tool call/)).toBeVisible(ANSWER);
  await expect(panel.getByRole("button", { name: /^(Approve|Reject): / })).toHaveCount(0);
  const selection = await page.request.get("/api/selection");
  expect(await selection.json()).toEqual({ entity_ids: [DESK] });
  await page.waitForTimeout(4000);
  await expect(deskCard(page)).toBeVisible();
});

/** The proof's Compose arguments, which it passes as a JSON list of strings. */
function parseComposeArgs(raw: string): string[] {
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed) || !parsed.every((arg): arg is string => typeof arg === "string")) {
    throw new Error(`E2E_COMPOSE_ARGS is not a JSON list of strings: ${raw}`);
  }
  return parsed;
}

test("reports an unreachable controller after you approve, while the card keeps the real state", async ({
  page,
}) => {
  // The replay wait, retries, a container stop and a 5 s command wait exceed the default 30 s.
  test.setTimeout(180_000);
  // The proof's Compose command, so the test can stop the controller mid-approval.
  const composeArgs = process.env.E2E_COMPOSE_ARGS;
  test.skip(!composeArgs, "E2E_COMPOSE_ARGS is not set: run through compose_agent_panel_proof.py");
  const composeBase = parseComposeArgs(composeArgs ?? "[]");
  const compose = (...args: string[]) =>
    execFileSync("docker", ["compose", ...composeBase, ...args], {
      stdio: "inherit",
      timeout: 60_000,
    });

  await page.goto("/");
  await expect(page.getByText("CONTROL ON")).toBeVisible(POLL);
  const panel = await openPanel(page);
  // A demo replay phase can flip the lamp between the read and the ask, so the agent's
  // pre-check may answer "Not sent"; ask again until a card appears.
  let approve: Locator | null = null;
  const refusals: string[] = [];
  for (let attempt = 0; attempt < 10 && !approve; attempt += 1) {
    // The demo replay holds every lamp unavailable for about 30 s, and the agent rightly
    // refuses to switch an unavailable lamp; wait for a known on/off state.
    await expect.poll(() => deskState(page), { timeout: 60_000 }).toMatch(/^(on|off)$/);
    const state = await deskState(page);
    const action = state === "on" ? "off" : "on";
    await panel.getByRole("button", { name: "New chat" }).click();
    // Ask only in the empty new chat, so an earlier chat's refusal cannot match.
    await expect(panel.getByText("Ask about your home")).toBeVisible();
    await ask(panel, `turn_${action} ${DESK}`);
    const button = panel.getByRole("button", { name: `Approve: Turn ${action} ${DESK_NAME}` });
    const refused = panel.getByText(/Result: Not sent:/);
    await expect(button.or(refused)).toBeVisible(ANSWER);
    if (await button.isVisible()) approve = button;
    else {
      // The refusal streams in; read it once its sentence is complete.
      await expect(refused).toHaveText(/\.\s*$/, ANSWER);
      refusals.push(await refused.innerText());
    }
  }
  if (!approve) throw new Error(`The agent never proposed a command: ${refusals.join(" | ")}`);

  // Control stays on for 15 seconds after the controller's last heartbeat, so the keeper
  // still accepts the command and then cannot reach the controller.
  compose("stop", "controller");
  try {
    await approve.click();
    await expect(panel.getByText(/Result: Cannot reach the light controller/)).toBeVisible(ANSWER);
    // The card shows what the keeper reads, not what the agent asked for.
    await expect
      .poll(async () => {
        const shown = await deskCard(page).locator("[data-status]").getAttribute("data-status");
        const state = await deskState(page);
        // The card marks anything but on and off, such as unavailable, as unknown.
        return shown === (state === "on" || state === "off" ? state : "unknown");
      }, POLL)
      .toBe(true);
  } finally {
    compose("start", "controller");
  }
});
