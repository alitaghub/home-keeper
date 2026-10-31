import { describe, expect, it } from "vitest";
import { adaptAegraStream } from "./stream";

type Event = { event: string; data: unknown };

async function adapt(events: Event[]): Promise<Event[]> {
  async function* source() {
    yield* events;
  }
  const out: Event[] = [];
  for await (const event of adaptAegraStream(source())) out.push(event);
  return out;
}

const step = (langgraph_step: number, run_id = "run-1") => ({
  run_id,
  langgraph_step,
  langgraph_node: "model",
  thread_id: "thread-1",
});

const messageIds = (events: Event[]) =>
  events.flatMap((event) => {
    const [message] = event.event === "messages" && Array.isArray(event.data) ? event.data : [];
    return message && typeof message === "object" && "id" in message ? [message.id] : [];
  });

describe("adaptAegraStream", () => {
  it("gives every AI chunk of one step the first id seen, so a Responses API reply shows once", async () => {
    // Recorded shape: OpenAI's Responses API streams one model step under two ids.
    const out = await adapt([
      { event: "metadata", data: { run_id: "run-1", attempt: 1 } },
      {
        event: "messages",
        data: [{ type: "AIMessageChunk", id: "lc_run--a1", content: "The desk " }, step(2)],
      },
      {
        event: "messages",
        data: [{ type: "AIMessageChunk", id: "resp_0a1", content: "lamp is " }, step(2)],
      },
      { event: "messages", data: [{ type: "ai", id: "resp_0a1", content: "on." }, step(2)] },
      // A later step is a separate reply and keeps its own first id.
      {
        event: "messages",
        data: [{ type: "AIMessageChunk", id: "lc_run--b2", content: "Anything else?" }, step(4)],
      },
      {
        event: "messages",
        data: [{ type: "AIMessageChunk", id: "resp_0b2", content: "" }, step(4)],
      },
      // The same step number in another run is another reply.
      {
        event: "messages",
        data: [{ type: "AIMessageChunk", id: "resp_0c3", content: "Hi" }, step(2, "run-2")],
      },
    ]);

    expect(messageIds(out)).toEqual([
      "lc_run--a1",
      "lc_run--a1",
      "lc_run--a1",
      "lc_run--b2",
      "lc_run--b2",
      "resp_0c3",
    ]);
    // Only the id changes: content and metadata pass through.
    expect(out[2]).toEqual({
      event: "messages",
      data: [{ type: "AIMessageChunk", id: "lc_run--a1", content: "lamp is " }, step(2)],
    });
  });

  it("passes a tool message chunk on as a tool message", async () => {
    const chunk = {
      type: "ToolMessageChunk",
      id: "tool-msg-1",
      content: 'Now monitoring "light.hue_desk". 1 device(s) monitored.',
      tool_call_id: "call-1",
      name: "monitor",
      status: "success",
    };
    const metadata = { ...step(3), langgraph_node: "tools" };

    const out = await adapt([{ event: "messages", data: [chunk, metadata] }]);

    expect(out).toEqual([{ event: "messages", data: [{ ...chunk, type: "tool" }, metadata] }]);
  });

  it("keeps only the interrupt of an update, and turns any other update into an empty one", async () => {
    const interrupt = [
      {
        id: "interrupt-1",
        value: {
          action_requests: [{ name: "monitor", args: { entity_id: "light.hue_desk" } }],
          review_configs: [{ action_name: "monitor", allowed_decisions: ["approve", "reject"] }],
        },
      },
    ];

    const out = await adapt([
      {
        event: "updates",
        data: { model: { messages: [{ type: "ai", id: "resp_1", content: "" }] } },
      },
      {
        event: "updates",
        data: { model: { messages: [] }, __interrupt__: interrupt },
      },
      {
        event: "updates",
        data: { tools: { messages: [{ type: "tool", id: "t1", content: "ok" }] } },
      },
    ]);

    expect(out).toEqual([
      { event: "updates", data: {} },
      { event: "updates", data: { __interrupt__: interrupt } },
      { event: "updates", data: {} },
    ]);
  });

  it("passes every other event on unchanged", async () => {
    const events: Event[] = [
      { event: "metadata", data: { run_id: "run-1", attempt: 1 } },
      { event: "values", data: { messages: [{ type: "human", id: "h1", content: "hi" }] } },
      { event: "messages", data: [{ type: "human", id: "h1", content: "hi" }, step(1)] },
      { event: "error", data: { error: "ValueError", message: "boom" } },
      { event: "end", data: { status: "success" } },
    ];

    expect(await adapt(events)).toEqual(events);
  });
});
