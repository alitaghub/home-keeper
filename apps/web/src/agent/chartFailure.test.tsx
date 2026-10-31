// A chart that cannot load or render must break only its own card, never the panel.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createFakeAegra, installFakeAegra } from "../test/fake-aegra";
import { installFakeApi } from "../test/fake-api";

const BAR = {
  kind: "bar",
  title: "Switch-ons this week",
  categories: ["Desk lamp", "Hall"],
  series: [{ name: "This week", values: [14, 9] }],
};

const panel = () =>
  screen.queryByRole("region", { name: "Agent" }) ??
  screen.getByRole("complementary", { name: "Agent" });

beforeEach(() => {
  localStorage.clear();
  vi.resetModules();
  // React reports the caught error on the console; the test checks what the user sees.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.doUnmock("./ChartToolUI");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

it.each([
  [
    "its code fails to load",
    () => {
      throw new TypeError("Failed to fetch dynamically imported module");
    },
  ],
  [
    "it fails to render",
    () => ({
      ChartToolUI: () => {
        throw new Error("Recharts could not draw");
      },
    }),
  ],
])(
  "shows an error in the chart's card and keeps the panel working when %s",
  async (_case, chartModule) => {
    vi.doMock("./ChartToolUI", chartModule);
    const { default: AgentPanel } = await import("./AgentPanel");
    installFakeApi();
    installFakeAegra(
      createFakeAegra({
        onRun: ({ question }) =>
          question === "Chart it"
            ? [
                { tool: { name: "show_chart", args: { spec: BAR } }, result: "Chart shown." },
                { say: "Here it is." },
              ]
            : question
              ? [{ say: `Reply: ${question}` }]
              : [],
      }),
    );
    const user = userEvent.setup();
    render(<AgentPanel names={new Map()} onClose={() => {}} />);
    const input = await waitFor(() => within(panel()).getByRole("textbox"));

    await user.type(input, "Chart it{Enter}");

    await waitFor(
      () => {
        const alert = within(panel())
          .queryAllByRole("alert")
          .find((element) => /chart/i.test(element.textContent ?? ""));
        expect(alert).toBeDefined();
      },
      { timeout: 5000 },
    );
    // The rest of the chat is still there, and the composer still takes a question.
    expect(within(panel()).getByText("Chart it")).toBeVisible();
    expect(await within(panel()).findByText("Here it is.")).toBeVisible();
    await user.type(within(panel()).getByRole("textbox"), "Still there?{Enter}");
    expect(await within(panel()).findByText("Reply: Still there?")).toBeVisible();
  },
  15_000,
);
