import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ai,
  createFakeAegra,
  type FakeAegra,
  human,
  installFakeAegra,
  type RunContext,
  type Step,
} from "../test/fake-aegra";
import AgentPanel from "./AgentPanel";

const names: ReadonlyMap<string, string> = new Map([["light.hue_desk", "Desk lamp"]]);
const NOT_RUNNING = "The agent isn't running. Start it with --profile agent.";

let aegra: FakeAegra;

function renderPanel(onRun?: (run: RunContext) => Step[]) {
  aegra = createFakeAegra(onRun ? { onRun } : {});
  installFakeAegra(aegra);
  return mountPanel();
}

/** Mounts the panel as the dashboard does, under StrictMode, against the current fake Aegra. */
function mountPanel() {
  const user = userEvent.setup();
  const view = render(
    <StrictMode>
      <AgentPanel names={names} onClose={() => {}} />
    </StrictMode>,
  );
  return { user, view };
}

/** The panel's landmark: a region or a complementary landmark named "Agent". */
const panel = () =>
  screen.queryByRole("region", { name: "Agent" }) ??
  screen.getByRole("complementary", { name: "Agent" });

async function ask(user: ReturnType<typeof userEvent.setup>, question: string) {
  const input = await waitFor(() => within(panel()).getByRole("textbox"));
  await user.type(input, `${question}{Enter}`);
}

async function openRecent(user: ReturnType<typeof userEvent.setup>, title: string) {
  await user.click(await screen.findByRole("button", { name: "Recent chats" }));
  await user.click(await screen.findByRole("button", { name: title }));
}

/** Waits a little so a second load or a duplicate render would have happened. */
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 150)));

/** How many times a reply of the fake agent ("Reply: …") appears in the panel's text. */
const repliesShown = () => (panel().textContent ?? "").split("Reply:").length - 1;

const decisionButtons = () => screen.queryAllByRole("button", { name: /^(Approve|Reject): / });
const assistantUiApprovals = () => screen.queryAllByRole("button", { name: /^(Allow|Deny)\b/ });

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("AgentPanel", () => {
  it("streams a reply once although the model changes its id mid-step", async () => {
    const { user } = renderPanel();

    await ask(user, "Which lights are on?");

    expect(await within(panel()).findByText("Reply: Which lights are on?")).toBeInTheDocument();
    await settle();
    // One reply: no second message holding part of it under the other id.
    expect(repliesShown()).toBe(1);
    expect(within(panel()).getAllByText("Which lights are on?")).toHaveLength(1);
    // The question went to Aegra on a thread the dashboard created for itself.
    const [run] = aegra.runs;
    expect(aegra.threads.get(run?.threadId ?? "")?.metadata.source).toBe("dashboard");
  });

  it("shows a tool call collapsed with its result, not waiting", async () => {
    const result = "Control: on\n- light.hue_desk: on";
    const { user } = renderPanel(({ question }) =>
      question
        ? [{ tool: { name: "list_monitored", args: {} }, result }, { say: "The desk lamp is on." }]
        : [],
    );

    await ask(user, "Which lights are on?");

    await within(panel()).findByText("The desk lamp is on.");
    expect(within(panel()).queryByText(/waiting/i)).toBeNull();
    expect(within(panel()).queryByText(/Control: on/)).toBeNull();
    await user.click(within(panel()).getByRole("button", { name: /list_monitored/ }));
    expect(await within(panel()).findByText(/Control: on/)).toBeVisible();
  });

  it("restores the open chat after a reload with each message once and one load", async () => {
    const first = renderPanel();
    await ask(first.user, "Which lights are on?");
    await within(panel()).findByText("Reply: Which lights are on?");
    const threadId = aegra.runs[0]?.threadId;
    first.view.unmount();
    const loadsBefore = aegra.requests.length;

    // A reload: the same browser storage, a fresh React tree.
    mountPanel();

    expect(await within(panel()).findByText("Reply: Which lights are on?")).toBeInTheDocument();
    await settle();
    expect(repliesShown()).toBe(1);
    expect(within(panel()).getAllByText("Which lights are on?")).toHaveLength(1);
    const loads = aegra.requests
      .slice(loadsBefore)
      .filter(
        (request) =>
          request.path === `/threads/${threadId}/state` ||
          request.path === `/threads/${threadId}/history`,
      );
    expect(loads).toHaveLength(1);
  });

  it("asks for each proposed action separately and resumes once with every decision in order", async () => {
    const { user } = renderPanel(({ question, resume }) => {
      if (question === "Set up the lamps") {
        return [
          {
            propose: [
              { name: "monitor", args: { entity_id: "light.hue_desk" } },
              { name: "turn_on", args: { entity_id: "light.hue_reading", brightness_percent: 30 } },
            ],
          },
        ];
      }
      if (resume !== undefined) {
        return [
          {
            results: [
              'Now monitoring "light.hue_desk". 1 device(s) monitored.',
              "User rejected the tool call for `turn_on`.",
            ],
          },
          { say: "Monitoring the desk lamp. The reading light did not change." },
        ];
      }
      return [];
    });

    await ask(user, "Set up the lamps");

    // Names come from the dashboard's data; an unknown device shows its entity id.
    const approveDesk = await screen.findByRole("button", {
      name: "Approve: Start monitoring Desk lamp",
    });
    expect(
      screen.getByRole("button", { name: "Reject: Start monitoring Desk lamp" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Approve: Turn on light.hue_reading at 30%" }),
    ).toBeVisible();
    expect(assistantUiApprovals()).toEqual([]);

    // Deciding the second action first: nothing is sent until both have a decision.
    await user.click(
      screen.getByRole("button", { name: "Reject: Turn on light.hue_reading at 30%" }),
    );
    expect(
      screen.queryByRole("button", { name: /: Turn on light\.hue_reading at 30%$/ }),
    ).toBeNull();
    await settle();
    expect(aegra.runs).toHaveLength(1);

    await user.click(approveDesk);

    await waitFor(() => expect(aegra.runs).toHaveLength(2));
    expect(aegra.runs[1]?.body.command).toEqual({
      resume: { decisions: [{ type: "approve" }, { type: "reject" }] },
    });
    expect(
      await within(panel()).findByText(
        "Monitoring the desk lamp. The reading light did not change.",
      ),
    ).toBeInTheDocument();
    await settle();
    expect(decisionButtons()).toEqual([]);
    expect(assistantUiApprovals()).toEqual([]);
    expect(aegra.runs).toHaveLength(2);
  });

  it("keeps a pending approval in its own chat when you switch chats", async () => {
    const { user } = renderPanel();
    aegra.seed({
      metadata: { source: "dashboard", thread_name: "Watch the desk lamp" },
      messages: [human("Watch the desk lamp")],
      propose: [{ name: "monitor", args: { entity_id: "light.hue_desk" } }],
    });
    aegra.seed({
      metadata: { source: "dashboard", thread_name: "Which lights are on?" },
      messages: [human("Which lights are on?"), ai("None of them.")],
    });

    await openRecent(user, "Watch the desk lamp");
    expect(
      await screen.findByRole("button", { name: "Approve: Start monitoring Desk lamp" }),
    ).toBeVisible();

    await openRecent(user, "Which lights are on?");
    await within(panel()).findByText("None of them.");
    await settle();
    expect(decisionButtons()).toEqual([]);

    await user.click(screen.getByRole("button", { name: "New chat" }));
    await settle();
    expect(decisionButtons()).toEqual([]);

    // The approval still belongs to its own chat.
    await openRecent(user, "Watch the desk lamp");
    expect(
      await screen.findByRole("button", { name: "Approve: Start monitoring Desk lamp" }),
    ).toBeVisible();
    expect(aegra.runs).toEqual([]);
  });

  it("lists only dashboard chats in Recent chats, newest first", async () => {
    const { user } = renderPanel();
    aegra.seed({
      metadata: { source: "dashboard", thread_name: "How long was the desk lamp on?" },
      messages: [human("How long was the desk lamp on?"), ai("2h.")],
    });
    // Telegram threads carry no source; neither does LangGraph Studio.
    aegra.seed({ metadata: { thread_name: "A Telegram question" } });
    aegra.seed({
      metadata: { source: "dashboard", thread_name: "Which lights are on?" },
      messages: [human("Which lights are on?"), ai("None.")],
    });
    const titles = ["Which lights are on?", "How long was the desk lamp on?"];

    await user.click(await screen.findByRole("button", { name: "Recent chats" }));

    await screen.findByRole("button", { name: "Which lights are on?" });
    const shown = screen
      .getAllByRole("button")
      .map((button) => button.textContent?.trim())
      .filter((name) => name !== undefined && [...titles, "A Telegram question"].includes(name));
    expect(shown).toEqual(titles);
  });

  it("deletes the open chat from Aegra and from Recent chats", async () => {
    const { user } = renderPanel();
    const thread = aegra.seed({
      metadata: { source: "dashboard", thread_name: "Which lights are on?" },
      messages: [human("Which lights are on?"), ai("None.")],
    });

    await openRecent(user, "Which lights are on?");
    await within(panel()).findByText("None.");
    await user.click(screen.getByRole("button", { name: "Delete chat" }));

    await waitFor(() => expect(aegra.threads.has(thread.thread_id)).toBe(false));
    await user.click(screen.getByRole("button", { name: "Recent chats" }));
    await settle();
    expect(screen.queryByRole("button", { name: "Which lights are on?" })).toBeNull();
  });

  it("shows an answer while it streams, before the run ends", async () => {
    const { user } = renderPanel();
    const release = aegra.pauseNextStream();

    await ask(user, "Which lights are on?");

    // The fake sends the first half of the reply, then waits.
    await waitFor(() => expect(panel()).toHaveTextContent("Reply: Which l"));
    expect(panel()).not.toHaveTextContent("Reply: Which lights are on?");
    release();
    expect(await within(panel()).findByText("Reply: Which lights are on?")).toBeInTheDocument();
  });

  it("asks again when a second approval arrives in the same chat, and resumes with only its decisions", async () => {
    const { user } = renderPanel(({ question, resume }) => {
      if (question === "Set up the lamps") {
        return [
          {
            propose: [
              { name: "monitor", args: { entity_id: "light.hue_desk" } },
              { name: "monitor", args: { entity_id: "light.hue_reading" } },
            ],
          },
        ];
      }
      if (aegra.runs.length === 2) {
        return [
          {
            results: [
              'Now monitoring "light.hue_desk". 1 device(s) monitored.',
              "User rejected the tool call for `monitor`.",
            ],
          },
          { propose: [{ name: "turn_off", args: { entity_id: "light.hue_desk" } }] },
        ];
      }
      return resume === undefined
        ? []
        : [{ results: ["Sent to light.hue_desk, but no change was seen within 5 seconds."] }];
    });

    await ask(user, "Set up the lamps");
    await user.click(
      await screen.findByRole("button", { name: "Approve: Start monitoring Desk lamp" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Reject: Start monitoring light.hue_reading" }),
    );

    const turnOff = await screen.findByRole("button", { name: "Approve: Turn off Desk lamp" });
    expect(decisionButtons()).toHaveLength(2);
    await user.click(turnOff);

    await waitFor(() => expect(aegra.runs).toHaveLength(3));
    expect(aegra.runs[1]?.body.command).toEqual({
      resume: { decisions: [{ type: "approve" }, { type: "reject" }] },
    });
    expect(aegra.runs[2]?.body.command).toEqual({
      resume: { decisions: [{ type: "approve" }] },
    });
    await settle();
    expect(decisionButtons()).toEqual([]);
  });

  it("brings the cards back when sending the decisions fails, and sends them again", async () => {
    const { user } = renderPanel(({ question, resume }) => {
      if (question === "Watch the desk lamp") {
        return [{ propose: [{ name: "monitor", args: { entity_id: "light.hue_desk" } }] }];
      }
      return resume === undefined ? [] : [{ say: "Monitoring the desk lamp." }];
    });
    await ask(user, "Watch the desk lamp");
    const approve = { name: "Approve: Start monitoring Desk lamp" };
    // Aegra refuses the resume, for example while the thread is still busy.
    aegra.failNextRun(409);

    await user.click(await screen.findByRole("button", approve));

    expect(await within(panel()).findByRole("alert")).toBeVisible();
    await user.click(await screen.findByRole("button", approve));
    expect(await within(panel()).findByText("Monitoring the desk lamp.")).toBeInTheDocument();
    const resumes = aegra
      .requestsTo("POST", /\/runs\/stream$/)
      .filter(
        (request) => request.body && typeof request.body === "object" && "command" in request.body,
      );
    expect(resumes).toHaveLength(2);
    expect(aegra.runs.at(-1)?.body.command).toEqual({
      resume: { decisions: [{ type: "approve" }] },
    });
  });

  it("shows an error and keeps the question when a run fails, and takes the next question", async () => {
    const { user } = renderPanel();
    await ask(user, "Which lights are on?");
    await within(panel()).findByText("Reply: Which lights are on?");
    // Aegra refuses the run, for example while the thread is still busy.
    aegra.failNextRun(409);

    await ask(user, "Is the desk lamp on?");

    expect(await within(panel()).findByRole("alert")).toBeVisible();
    // The question survives, in the conversation or back in the composer, to send again.
    const input = within(panel()).getByRole("textbox");
    const kept =
      (input instanceof HTMLTextAreaElement && input.value === "Is the desk lamp on?") ||
      within(panel()).queryAllByText("Is the desk lamp on?").length > 0;
    expect(kept).toBe(true);
    await user.clear(input);
    await ask(user, "Try again");
    expect(await within(panel()).findByText("Reply: Try again")).toBeInTheDocument();
  });

  it("brings the cards back when the resumed run streams an error", async () => {
    const { user } = renderPanel(({ question, resume }) => {
      if (question === "Watch the desk lamp") {
        return [{ propose: [{ name: "monitor", args: { entity_id: "light.hue_desk" } }] }];
      }
      return resume === undefined ? [] : [{ say: "Monitoring the desk lamp." }];
    });
    await ask(user, "Watch the desk lamp");
    const approve = { name: "Approve: Start monitoring Desk lamp" };
    // Aegra accepts the resume, then the graph fails before the tool runs.
    aegra.errorNextRun("temporary graph failure");

    await user.click(await screen.findByRole("button", approve));

    expect(await within(panel()).findByText(/temporary graph failure/)).toBeVisible();
    await user.click(await screen.findByRole("button", approve));
    expect(await within(panel()).findByText("Monitoring the desk lamp.")).toBeInTheDocument();
    expect(aegra.runs.at(-1)?.body.command).toEqual({
      resume: { decisions: [{ type: "approve" }] },
    });
  });

  it("moves a chat to the top of Recent chats once it gets a new answer", async () => {
    const { user } = renderPanel();
    aegra.seed({
      metadata: { source: "dashboard", thread_name: "Older question" },
      messages: [human("Older question"), ai("Older answer")],
    });
    aegra.seed({
      metadata: { source: "dashboard", thread_name: "Newer question" },
      messages: [human("Newer question"), ai("Newer answer")],
    });
    const titles = ["Older question", "Newer question"];
    const recentOrder = () =>
      screen
        .getAllByRole("button")
        .map((button) => button.textContent?.trim())
        .filter((name) => name !== undefined && titles.includes(name));

    await openRecent(user, "Older question");
    await within(panel()).findByText("Older answer");
    await ask(user, "Follow up");
    await within(panel()).findByText("Reply: Follow up");
    await settle();

    await user.click(screen.getByRole("button", { name: "Recent chats" }));
    await waitFor(() => expect(recentOrder()).toEqual(["Older question", "Newer question"]));
  });

  it("starts a new chat and forgets a saved chat Aegra no longer has", async () => {
    const gone = "thread-deleted-elsewhere";
    localStorage.setItem("home-keeper.agent-thread", gone);
    const first = renderPanel();

    await waitFor(() => expect(within(panel()).getByRole("textbox")).toBeEnabled());
    await waitFor(() => expect(aegra.requestsTo("GET", `/threads/${gone}`)).not.toEqual([]));
    await settle();
    expect(localStorage.getItem("home-keeper.agent-thread")).toBeNull();
    first.view.unmount();
    const before = aegra.requests.length;

    const second = mountPanel();
    await waitFor(() => expect(within(panel()).getByRole("textbox")).toBeEnabled());
    await settle();

    expect(aegra.requests.slice(before).filter((request) => request.path.includes(gone))).toEqual(
      [],
    );
    // The new chat takes a question.
    await ask(second.user, "Which lights are on?");
    expect(await within(panel()).findByText("Reply: Which lights are on?")).toBeInTheDocument();
    expect(aegra.runs[0]?.threadId).not.toBe(gone);
  });

  it("loads older chats in Recent chats page by page", async () => {
    const { user } = renderPanel();
    // One more chat than a page of Recent chats holds, oldest first.
    for (let index = 0; index <= 50; index += 1) {
      aegra.seed({ metadata: { source: "dashboard", thread_name: `Question ${index}` } });
    }

    await user.click(await screen.findByRole("button", { name: "Recent chats" }));
    await screen.findByRole("button", { name: "Question 50" });
    expect(screen.queryByRole("button", { name: "Question 0" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Load more chats" }));

    expect(await screen.findByRole("button", { name: "Question 0" })).toBeVisible();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Load more chats" })).toBeNull(),
    );
  });

  it("says the agent isn't running when its health check does not answer in 5 seconds", async () => {
    aegra = createFakeAegra();
    aegra.hold("GET", "/health");
    installFakeAegra(aegra);
    mountPanel();

    await waitFor(() => expect(panel()).toHaveTextContent(NOT_RUNNING), { timeout: 8000 });
  }, 10_000);

  it.each(["network", 502] as const)(
    "says the agent isn't running and how to start it when Aegra answers %s",
    async (failure) => {
      aegra = createFakeAegra();
      aegra.fail(failure);
      installFakeAegra(aegra);
      mountPanel();

      // Promptly: a user should not wait out retries to learn the agent is off.
      await waitFor(() => expect(panel()).toHaveTextContent(NOT_RUNNING), { timeout: 3000 });
      const link = within(panel()).getByRole("link", { name: "How to start the agent" });
      expect(link.getAttribute("href")).toMatch(/docs\/agent\.md/);
    },
  );
});
