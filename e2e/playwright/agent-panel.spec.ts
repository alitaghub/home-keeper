// The agent panel against the real agent graph in Aegra, with a deterministic model
// (e2e/dashboard_graph.py), behind the dashboard's own address. Started by
// e2e/compose_agent_panel_proof.py; never against a real home.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import {
  type Download,
  expect,
  type Locator,
  type Page,
  type Response,
  test,
} from "@playwright/test";
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

const WIDE = { width: 1440, height: 900 };

/** The panel's width, once it has one. */
const widthOf = (panel: Locator) => async () => (await panel.boundingBox())?.width ?? 0;

/** Opens the panel on a wide window with no remembered width. */
async function openFresh(page: Page): Promise<Locator> {
  await page.setViewportSize(WIDE);
  await page.goto("/");
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  const panel = await openPanel(page);
  await expect(panel.getByRole("textbox")).toBeVisible();
  return panel;
}

/** Drags the panel's resize handle sideways by `dx` pixels. */
async function dragHandle(page: Page, dx: number) {
  const box = await page.getByRole("separator", { name: "Resize agent panel" }).boundingBox();
  expect(box).toBeTruthy();
  if (!box) return;
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width / 2, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + dx, y, { steps: 10 });
  await page.mouse.up();
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
    // 40% of the window by default.
    expect(box.width).toBeGreaterThan(1440 * 0.38);
    expect(box.width).toBeLessThan(1440 * 0.42);
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

test("resizes by dragging or with the arrow keys, and remembers its width", async ({ page }) => {
  const panel = await openFresh(page);
  const width = widthOf(panel);
  const start = await width();

  await dragHandle(page, -200);

  const dragged = await width();
  expect(Math.abs(dragged - start - 200)).toBeLessThanOrEqual(10);
  await page.reload();
  await openPanel(page);
  await expect.poll(width).toBeGreaterThan(dragged - 5);
  expect(await width()).toBeLessThan(dragged + 5);
  await page.getByRole("separator", { name: "Resize agent panel" }).focus();
  await page.keyboard.press("ArrowLeft");
  await expect.poll(width).toBeGreaterThan(dragged + 5);
});

test("stays between 360 px and 70% of the window, beside the cards", async ({ page }) => {
  const panel = await openFresh(page);
  const width = widthOf(panel);

  await dragHandle(page, 1000);
  await expect.poll(width).toBeGreaterThanOrEqual(359);
  expect(await width()).toBeLessThan(370);
  await dragHandle(page, -2000);

  await expect.poll(width).toBeGreaterThan(1440 * 0.7 - 5);
  expect(await width()).toBeLessThanOrEqual(1440 * 0.7 + 2);
  const home = await page.locator("#panel-home").boundingBox();
  const box = await panel.boundingBox();
  expect(home && box).toBeTruthy();
  if (home && box) expect(home.x + home.width).toBeLessThanOrEqual(box.x);
});

test("expands to most of the window and goes back to the width you had", async ({ page }) => {
  const panel = await openFresh(page);
  const width = widthOf(panel);
  const before = await width();

  await panel.getByRole("button", { name: "Expand agent panel" }).click();

  await expect.poll(width).toBeGreaterThan(1440 * 0.7 - 5);
  await panel.getByRole("button", { name: "Restore agent panel width" }).click();
  await expect.poll(width).toBeLessThan(before + 5);
  expect(await width()).toBeGreaterThan(before - 5);
});

test("is a full-screen sheet on a phone that closes back to the tab", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/#history");
  const panel = await openPanel(page);
  const box = await panel.boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(370);
  // The sheet already fills the phone: it has nothing to resize or expand.
  await expect(page.getByRole("separator", { name: "Resize agent panel" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Expand agent panel" })).toHaveCount(0);

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

// Charts: the deterministic model answers "bar", "line", "badchart" and "usage <ids>" with a
// show_chart call (e2e/dashboard_graph.py). These run after the tests above, so the desk
// lamp is monitored and has history.
const BAR_TITLE = "Switch-ons this week";
const LINE_TITLE = "Desk lamp on-time by hour";
const MARKUP_NAME = "<b>Reading</b> light";
const CANNOT_DRAW = "The agent sent a chart the panel cannot draw.";
// Download names: <title-slug>-<YYYY-MM-DD>.<ext>.
const fileName = (slug: string, extension: string) =>
  new RegExp(`^${slug}-\\d{4}-\\d{2}-\\d{2}\\.${extension}$`);
// Chart screenshots in light and dark, kept as evidence after a run: by default
// apps/web/test-results/agent-charts, as the e2e task runs Playwright from apps/web.
const SCREENSHOTS = path.resolve(
  process.env.E2E_SCREENSHOT_DIR ?? path.join("test-results", "agent-charts"),
);

/** The chart whose accessible name, a short summary, mentions `text`. */
const chartNamed = (panel: Locator, text: string | RegExp) =>
  panel
    .getByRole("img", { name: text })
    .or(panel.getByRole("figure", { name: text }))
    .first();

/** Collects Content-Security-Policy violations the page reports from now on. */
async function watchCsp(page: Page): Promise<string[]> {
  const violations: string[] = [];
  page.on("console", (message) => {
    if (/Content Security Policy|Refused to|CSP violation/i.test(message.text())) {
      violations.push(message.text());
    }
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      console.error(`CSP violation: ${event.violatedDirective} ${event.blockedURI}`);
    });
  });
  return violations;
}

/** Opens the panel in `scheme` on a new chat. */
async function openChartChat(page: Page, scheme: "light" | "dark" = "light") {
  await page.emulateMedia({ colorScheme: scheme });
  await page.setViewportSize(WIDE);
  await page.goto("/");
  const panel = await openPanel(page);
  await panel.getByRole("button", { name: "New chat" }).click();
  await expect(panel.getByRole("textbox")).toBeVisible();
  return panel;
}

async function chooseInMenu(page: Page, panel: Locator, item: string) {
  await panel.getByRole("button", { name: /options/i }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

async function downloadFrom(page: Page, panel: Locator, item: string): Promise<Download> {
  const download = page.waitForEvent("download");
  await chooseInMenu(page, panel, item);
  return download;
}

async function fileOf(download: Download): Promise<Buffer> {
  const saved = await download.path();
  expect(saved).toBeTruthy();
  return readFileSync(saved ?? "");
}

/**
 * A PNG decoded by the browser itself: its size, how many colours it has, its corner pixel,
 * and how many of its pixels have each of `targets`' RGB colours.
 */
async function decodePng(page: Page, bytes: Buffer, targets: number[][] = []) {
  // The PNG's IHDR chunk holds its width and height right after the 8-byte signature.
  expect(bytes.subarray(0, 8)).toEqual(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  // A blank page, outside the dashboard's CSP, decodes it.
  const blank = await page.context().newPage();
  try {
    return await blank.evaluate(
      async ([base64, wanted]) => {
        const image = new Image();
        image.src = `data:image/png;base64,${base64}`;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const pen = canvas.getContext("2d");
        if (!pen) throw new Error("No 2D canvas");
        pen.drawImage(image, 0, 0);
        const { data } = pen.getImageData(0, 0, canvas.width, canvas.height);
        const colours = new Set<number>();
        // Antialiased edges blend; a bar's inside matches its colour within a few levels.
        const near = (index: number, [red = 0, green = 0, blue = 0]: number[]) =>
          Math.abs((data[index] ?? 0) - red) <= 8 &&
          Math.abs((data[index + 1] ?? 0) - green) <= 8 &&
          Math.abs((data[index + 2] ?? 0) - blue) <= 8;
        const matches = wanted.map(() => 0);
        for (let index = 0; index < data.length; index += 4) {
          wanted.forEach((colour, which) => {
            if (near(index, colour)) matches[which] = (matches[which] ?? 0) + 1;
          });
          colours.add(
            (((data[index] ?? 0) << 24) |
              ((data[index + 1] ?? 0) << 16) |
              ((data[index + 2] ?? 0) << 8) |
              (data[index + 3] ?? 0)) >>>
              0,
          );
        }
        return {
          width: canvas.width,
          height: canvas.height,
          colours: colours.size,
          corner: [data[0] ?? 0, data[1] ?? 0, data[2] ?? 0, data[3] ?? 0],
          matches,
        };
      },
      [bytes.toString("base64"), targets] as const,
    );
  } finally {
    await blank.close();
  }
}

/** The RGB colours the page's CSS variables resolve to, as a canvas paints them. */
function cssColours(page: Page, variables: string[]): Promise<number[][]> {
  return page.evaluate(
    (names) =>
      names.map((name) => {
        // Set through the CSSOM, which the CSP allows.
        const probe = document.createElement("div");
        probe.style.color = `var(${name})`;
        document.body.append(probe);
        const colour = getComputedStyle(probe).color;
        probe.remove();
        const canvas = document.createElement("canvas");
        canvas.width = 1;
        canvas.height = 1;
        const pen = canvas.getContext("2d");
        if (!pen) throw new Error("No 2D canvas");
        pen.fillStyle = colour;
        pen.fillRect(0, 0, 1, 1);
        return [...pen.getImageData(0, 0, 1, 1).data.slice(0, 3)];
      }),
    variables,
  );
}

/** The area, in CSS pixels, of the bars Recharts draws for each series, in series order. */
async function barAreas(panel: Locator): Promise<number[]> {
  const areas: number[] = [];
  for (const series of await panel.locator(".recharts-bar").all()) {
    let area = 0;
    for (const bar of await series.locator(".recharts-bar-rectangle").all()) {
      const box = await bar.boundingBox();
      area += box ? box.width * box.height : 0;
    }
    areas.push(area);
  }
  return areas;
}

const luminance = ([red = 0, green = 0, blue = 0]: number[]) =>
  (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255;

/** Keeps a screenshot of the chart's card as evidence of its look. */
async function keepScreenshot(panel: Locator, name: string) {
  mkdirSync(SCREENSHOTS, { recursive: true });
  await panel
    .getByRole("figure")
    .first()
    .screenshot({ path: path.join(SCREENSHOTS, `${name}.png`) });
}

test("loads the chart code only when the first chart arrives", async ({ page }) => {
  const scripts: Response[] = [];
  page.on("response", (response) => {
    if (response.request().resourceType() === "script") scripts.push(response);
  });
  const bundles = async (responses: Response[]) =>
    Promise.all(responses.map((response) => response.text().catch(() => "")));
  const hasRecharts = (bodies: string[]) =>
    bodies.some((body) => /recharts-(wrapper|surface)/.test(body));
  // No saved chat, so the open panel has no chart.
  const panel = await openFresh(page);
  await page.waitForLoadState("networkidle");
  const before = scripts.length;
  expect(hasRecharts(await bundles(scripts))).toBe(false);

  await ask(panel, "bar");

  await expect(chartNamed(panel, new RegExp(BAR_TITLE))).toBeVisible(ANSWER);
  const loaded = scripts.slice(before);
  expect(loaded.length).toBeGreaterThan(0);
  expect(hasRecharts(await bundles(loaded))).toBe(true);
});

test("draws a bar chart, shows a value on hover, and switches to a table and back", async ({
  page,
}) => {
  const violations = await watchCsp(page);
  const panel = await openChartChat(page);

  await ask(panel, "bar");

  await expect(chartNamed(panel, new RegExp(BAR_TITLE))).toBeVisible(ANSWER);
  await expect(panel.getByText("Here is the chart.")).toBeVisible(ANSWER);
  await expect(panel.getByText("This week", { exact: true })).toBeVisible();
  await expect(panel.getByText("Last week", { exact: true })).toBeVisible();
  // No inline <style>: the CSP would block it.
  await expect(panel.locator("style")).toHaveCount(0);
  // The first bar is this week's desk lamp; its tooltip shows the category's values.
  await panel.locator(".recharts-bar-rectangle").first().hover();
  const tooltip = panel.locator(".recharts-tooltip-wrapper");
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toContainText("14");
  await expect(tooltip).toContainText("11");
  await keepScreenshot(panel, "bar-light");

  await chooseInMenu(page, panel, "Show as table");

  const table = panel.getByRole("table");
  await expect(table).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "This week" })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "Last week" })).toBeVisible();
  await expect(panel.locator("svg.recharts-surface")).toHaveCount(0);
  await chooseInMenu(page, panel, "Show as chart");
  await expect(panel.getByRole("table")).toHaveCount(0);
  await expect(panel.locator("svg.recharts-surface")).toBeVisible();
  expect(violations).toEqual([]);
});

test("downloads a bar chart as CSV and as a PNG of the chart in the light theme", async ({
  page,
}) => {
  const violations = await watchCsp(page);
  const panel = await openChartChat(page);
  await ask(panel, "bar");
  await expect(chartNamed(panel, new RegExp(BAR_TITLE))).toBeVisible(ANSWER);
  // Let the bars finish growing before the PNG copies them.
  await page.waitForTimeout(1500);

  const csv = await downloadFrom(page, panel, "Download CSV");

  expect(csv.suggestedFilename()).toMatch(fileName("switch-ons-this-week", "csv"));
  // These cells hold no comma or quote, so a line splits on commas; a cell may be quoted.
  const [header, firstRow] = (await fileOf(csv))
    .toString("utf8")
    .replace(/^\uFEFF/, "")
    .split(/\r?\n/)
    .map((line) => line.split(",").map((cell) => cell.replace(/^"(.*)"$/, "$1")));
  expect(header?.slice(1)).toEqual(["This week", "Last week"]);
  expect(firstRow).toEqual(["Desk lamp", "14", "11"]);

  const png = await downloadFrom(page, panel, "Download PNG");

  expect(png.suggestedFilename()).toMatch(fileName("switch-ons-this-week", "png"));
  const seriesColours = await cssColours(page, ["--chart-1", "--chart-2"]);
  const areas = await barAreas(panel);
  const image = await decodePng(page, await fileOf(png), seriesColours);
  const plot = await panel.locator("svg.recharts-surface").boundingBox();
  expect(plot).toBeTruthy();
  const scale = Math.round(image.width / (plot?.width ?? 1));
  if (plot) {
    // The chart's size, at a whole device-pixel scale, plus room for the title and legend.
    expect(image.width / plot.width).toBeGreaterThan(0.95);
    expect(Math.abs(image.width / plot.width - scale)).toBeLessThan(0.15);
    expect(image.height).toBeGreaterThan(plot.height * scale);
  }
  // The bars themselves are painted: each series' colour covers about the area its bars
  // cover on screen. The title, text and legend swatches alone come nowhere near it.
  expect(areas).toHaveLength(2);
  for (const [index, area] of areas.entries()) {
    expect(area).toBeGreaterThan(2000);
    const painted = (image.matches[index] ?? 0) / (scale * scale);
    expect(painted).toBeGreaterThan(area * 0.75);
    expect(painted).toBeLessThan(area * 1.25);
  }
  // Not one flat colour: bars, text and grid are drawn.
  expect(image.colours).toBeGreaterThan(8);
  // The light theme's opaque, light background.
  expect(image.corner[3]).toBe(255);
  expect(luminance(image.corner)).toBeGreaterThan(0.7);
  expect(violations).toEqual([]);
});

test("draws a line chart with straight segments and a name with markup as text", async ({
  page,
}) => {
  const violations = await watchCsp(page);
  const panel = await openChartChat(page);

  await ask(panel, "line");

  await expect(chartNamed(panel, new RegExp(LINE_TITLE))).toBeVisible(ANSWER);
  const curves = panel.locator(".recharts-line-curve");
  await expect(curves).toHaveCount(2);
  for (const curve of await curves.all()) {
    // Cubic Bézier commands would mean a smoothed curve.
    expect(await curve.getAttribute("d")).not.toMatch(/[CcSsQqTt]/);
  }
  await expect(panel.getByText(MARKUP_NAME, { exact: true }).first()).toBeVisible();
  // Late evening, when the reading light is on.
  const plot = await panel.locator("svg.recharts-surface").boundingBox();
  expect(plot).toBeTruthy();
  if (plot) await page.mouse.move(plot.x + plot.width * 0.85, plot.y + plot.height / 2);
  const tooltip = panel.locator(".recharts-tooltip-wrapper");
  await expect(tooltip).toContainText(MARKUP_NAME);
  await expect(panel.locator("figure b")).toHaveCount(0);
  await keepScreenshot(panel, "line-light");
  await chooseInMenu(page, panel, "Show as table");
  await expect(panel.getByRole("columnheader", { name: MARKUP_NAME })).toBeVisible();
  expect(violations).toEqual([]);
});

test("draws the desk lamp's on and unknown time per day from its real history", async ({
  page,
}) => {
  const violations = await watchCsp(page);
  const panel = await openChartChat(page);

  await ask(panel, `usage ${DESK}`);

  // Named with the friendly name, not the entity ID.
  await expect(chartNamed(panel, new RegExp(DESK_NAME))).toBeVisible(ANSWER);
  await expect(panel.locator(".recharts-bar")).toHaveCount(2);
  await expect(panel.getByText(/on \d+(h|m)/).first()).toBeVisible();
  await expect(panel).not.toContainText(/from Home Assistant|source/i);
  await keepScreenshot(panel, "usage-light");
  await chooseInMenu(page, panel, "Show as table");
  // One row per day of the 7-day range, plus the header.
  expect(await panel.getByRole("table").getByRole("row").count()).toBeGreaterThanOrEqual(8);
  expect(violations).toEqual([]);
});

test("says it cannot draw a chart the agent's schema rejected", async ({ page }) => {
  const panel = await openChartChat(page);

  await ask(panel, "badchart");

  await expect(panel.getByText(CANNOT_DRAW)).toBeVisible(ANSWER);
  await expect(panel.getByText("That chart did not work.")).toBeVisible(ANSWER);
});

test("draws charts and their PNG in the dark theme", async ({ page }) => {
  const violations = await watchCsp(page);
  const panel = await openChartChat(page, "dark");

  for (const [question, name, shot] of [
    ["bar", BAR_TITLE, "bar-dark"],
    ["line", LINE_TITLE, "line-dark"],
    [`usage ${DESK}`, DESK_NAME, "usage-dark"],
  ] as const) {
    await panel.getByRole("button", { name: "New chat" }).click();
    await ask(panel, question);
    await expect(chartNamed(panel, new RegExp(name))).toBeVisible(ANSWER);
    await page.waitForTimeout(1500);
    await keepScreenshot(panel, shot);
  }
  const png = await downloadFrom(page, panel, "Download PNG");
  const image = await decodePng(page, await fileOf(png));

  expect(image.colours).toBeGreaterThan(8);
  expect(image.corner[3]).toBe(255);
  expect(luminance(image.corner)).toBeLessThan(0.3);
  expect(violations).toEqual([]);
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
