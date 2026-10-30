// An in-memory Aegra behind `fetch` for the agent panel's tests. Runs stream in Aegra's
// shapes: AI chunks that change id within a step, `ToolMessageChunk` tool results, approval
// interrupts in `updates`, and a closing `end` event.
import { vi } from "vitest";

export type ToolCall = { name: string; args: Record<string, unknown> };

type Message = { id: string; type: string; [key: string]: unknown };
type Interrupt = { id: string; value: unknown };

export type FakeThread = {
  thread_id: string;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
  messages: Message[];
  interrupt: Interrupt | null;
  // The tool calls the pending interrupt asks about, to answer after the resume.
  pendingCalls: { id: string; name: string }[];
};

/** What the scripted agent does in one run. */
export type Step =
  | { say: string }
  /** Asks for approval of these calls: the run ends interrupted. */
  | { propose: ToolCall[] }
  /** A tool the agent calls without approval, with its result. */
  | { tool: ToolCall; result: string }
  /** Tool results for the calls the resumed interrupt asked about, in order. */
  | { results: string[] };

export type RunContext = {
  thread: FakeThread;
  /** The text of the human message this run adds, if any. */
  question: string | undefined;
  /** The `command.resume` value, when the run resumes an interrupt. */
  resume: unknown;
};

export type RunRequest = { threadId: string; body: Record<string, unknown> };
export type AegraRequest = { method: string; path: string; body: unknown };
/** A dropped connection, or an HTTP error status, for every /agent request. */
export type AegraFailure = "network" | number;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const text = (content: unknown): string =>
  typeof content === "string"
    ? content
    : Array.isArray(content)
      ? content
          .map((part) => (isRecord(part) && typeof part.text === "string" ? part.text : ""))
          .join("")
      : "";

/** The interrupt value HumanInTheLoopMiddleware raises for these calls. */
export const approvalRequest = (calls: ToolCall[]) => ({
  action_requests: calls.map(({ name, args }) => ({
    name,
    args,
    description: `Tool execution requires approval\n\nTool: ${name}\nArgs: ${JSON.stringify(args)}`,
  })),
  review_configs: calls.map(({ name }) => ({
    action_name: name,
    allowed_decisions: ["approve", "reject"],
  })),
});

export function createFakeAegra({
  base = "/agent",
  onRun = ({ question }: RunContext): Step[] => (question ? [{ say: `Reply: ${question}` }] : []),
}: {
  base?: string;
  onRun?: (run: RunContext) => Step[];
} = {}) {
  const threads = new Map<string, FakeThread>();
  const requests: AegraRequest[] = [];
  const runs: RunRequest[] = [];
  let failure: AegraFailure | null = null;
  // One-shot status for the next run request; the thread stays as it was.
  let nextRunFailure: number | null = null;
  // One-shot graph error for the next run: Aegra accepts it, then streams `error`.
  let nextRunError: string | null = null;
  // Set, the next run pauses after its first text chunk until this promise settles.
  let streamGate: Promise<void> | null = null;
  const holds: { method: string; path: string; held: Promise<void> }[] = [];
  let clock = Date.parse("2026-10-30T09:00:00Z");
  let counter = 0;
  const now = () => {
    clock += 1000;
    return new Date(clock).toISOString();
  };
  const nextId = (prefix: string) => `${prefix}-${++counter}`;

  const threadJson = (thread: FakeThread) => ({
    thread_id: thread.thread_id,
    created_at: thread.created_at,
    updated_at: thread.updated_at,
    state_updated_at: thread.updated_at,
    metadata: thread.metadata,
    status: thread.interrupt ? "interrupted" : "idle",
    values: { messages: thread.messages },
    interrupts: thread.interrupt ? { "task-1": [thread.interrupt] } : {},
    config: {},
  });

  const stateJson = (thread: FakeThread) => {
    const tasks = thread.interrupt
      ? [
          {
            id: "task-1",
            name: "HumanInTheLoopMiddleware.after_model",
            interrupts: [thread.interrupt],
            error: null,
            checkpoint: null,
            state: null,
            result: null,
          },
        ]
      : [];
    return {
      values: { messages: thread.messages },
      next: tasks.map((task) => task.name),
      tasks,
      interrupts: thread.interrupt ? [thread.interrupt] : [],
      metadata: {},
      created_at: thread.updated_at,
      checkpoint: { thread_id: thread.thread_id, checkpoint_ns: "", checkpoint_id: nextId("cp") },
      parent_checkpoint: null,
      checkpoint_id: nextId("cp"),
      parent_checkpoint_id: null,
    };
  };

  /** Adds a thread as if an earlier session had left it in Aegra. */
  const seed = ({
    id = nextId("thread"),
    metadata = {},
    messages = [],
    propose,
  }: {
    id?: string;
    metadata?: Record<string, unknown>;
    messages?: Message[];
    /** Leaves the thread waiting for approval of these calls. */
    propose?: ToolCall[];
  }): FakeThread => {
    const at = now();
    const thread: FakeThread = {
      thread_id: id,
      created_at: at,
      updated_at: at,
      metadata: { owner: "noop", assistant_id: "agent", graph_id: "agent", ...metadata },
      messages: [...messages],
      interrupt: null,
      pendingCalls: [],
    };
    if (propose) {
      const calls = propose.map((call) => ({ ...call, id: nextId("call"), type: "tool_call" }));
      thread.messages.push({ id: nextId("resp"), type: "ai", content: "", tool_calls: calls });
      thread.interrupt = { id: nextId("interrupt"), value: approvalRequest(propose) };
      thread.pendingCalls = calls.map(({ id: callId, name }) => ({ id: callId, name }));
    }
    threads.set(id, thread);
    return thread;
  };

  const stream = (thread: FakeThread, body: Record<string, unknown>): Response => {
    const modes = new Set(
      Array.isArray(body.stream_mode)
        ? body.stream_mode
        : typeof body.stream_mode === "string"
          ? [body.stream_mode]
          : ["values"],
    );
    const runId = nextId("run");
    const events: string[] = [];
    // Index in `events` where a gated stream waits.
    let pauseAt: number | null = null;
    const emit = (event: string, data: unknown) =>
      events.push(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    let step = 0;
    const meta = (node: string) => ({
      run_id: runId,
      thread_id: thread.thread_id,
      langgraph_step: step,
      langgraph_node: node,
      langgraph_triggers: [`branch:to:${node}`],
      langgraph_checkpoint_ns: `${node}:${runId}`,
    });
    const tuples = modes.has("messages-tuple") || modes.has("messages");
    const add = (node: string, message: Message) => {
      thread.messages.push(message);
      if (modes.has("updates")) emit("updates", { [node]: { messages: [message] } });
      if (modes.has("values")) emit("values", { messages: thread.messages });
    };

    emit("metadata", { run_id: runId, attempt: 1 });
    const input = isRecord(body.input) ? body.input : {};
    const added = Array.isArray(input.messages) ? input.messages.filter(isRecord) : [];
    for (const message of added) {
      const human: Message = {
        ...message,
        id: typeof message.id === "string" ? message.id : nextId("human"),
        type: "human",
      };
      thread.messages.push(human);
      if (!thread.metadata.thread_name) thread.metadata.thread_name = text(human.content);
    }
    if (modes.has("values")) emit("values", { messages: thread.messages });
    const command = isRecord(body.command) ? body.command : {};
    const resume = command.resume;
    const pending = thread.pendingCalls;
    if (resume !== undefined) {
      thread.interrupt = null;
      thread.pendingCalls = [];
    }
    const question = added.length ? text(added.at(-1)?.content) : undefined;

    for (const action of onRun({ thread, question, resume })) {
      step += 1;
      if ("say" in action) {
        const id = nextId("resp");
        const half = Math.ceil(action.say.length / 2);
        if (tuples) {
          // The Responses API's first chunk carries LangChain's run id, the rest OpenAI's.
          emit("messages", [
            { type: "AIMessageChunk", id: `lc_run--${id}`, content: action.say.slice(0, half) },
            meta("model"),
          ]);
          pauseAt ??= events.length;
          emit("messages", [
            { type: "AIMessageChunk", id, content: action.say.slice(half) },
            meta("model"),
          ]);
        }
        add("model", { id, type: "ai", content: action.say, tool_calls: [] });
      } else if ("propose" in action || "tool" in action) {
        const proposed = "propose" in action ? action.propose : [action.tool];
        const calls = proposed.map((call) => ({ ...call, id: nextId("call"), type: "tool_call" }));
        const id = nextId("resp");
        if (tuples) {
          emit("messages", [
            {
              type: "AIMessageChunk",
              id: `lc_run--${id}`,
              content: "",
              tool_call_chunks: calls.map((call, index) => ({
                id: call.id,
                name: call.name,
                args: JSON.stringify(call.args),
                index,
                type: "tool_call_chunk",
              })),
            },
            meta("model"),
          ]);
        }
        add("model", { id, type: "ai", content: "", tool_calls: calls });
        if ("propose" in action) {
          thread.interrupt = { id: nextId("interrupt"), value: approvalRequest(action.propose) };
          thread.pendingCalls = calls.map(({ id: callId, name }) => ({ id: callId, name }));
          if (modes.has("updates")) emit("updates", { __interrupt__: [thread.interrupt] });
          if (modes.has("values")) {
            emit("values", { messages: thread.messages, __interrupt__: [thread.interrupt] });
          }
          break;
        }
        step += 1;
        const [call] = calls;
        if (call) addTool(call, action.result);
      } else {
        action.results.forEach((result, index) => {
          const call = pending[index];
          if (call) addTool(call, result);
        });
      }
    }
    thread.updated_at = now();
    emit("end", { status: thread.interrupt ? "interrupted" : "success" });

    function addTool(call: { id: string; name: string }, content: string) {
      const message: Message = {
        id: nextId("tool"),
        type: "tool",
        content,
        tool_call_id: call.id,
        name: call.name,
        status: "success",
      };
      if (tuples) emit("messages", [{ ...message, type: "ToolMessageChunk" }, meta("tools")]);
      add("tools", message);
    }

    const bytes = new TextEncoder();
    const gate = streamGate;
    streamGate = null;
    return new Response(
      new ReadableStream<Uint8Array>({
        async start(controller) {
          for (const [index, event] of events.entries()) {
            if (gate && index === pauseAt) await gate;
            controller.enqueue(bytes.encode(event));
          }
          controller.close();
        },
      }),
      {
        status: 200,
        headers: {
          "Content-Type": "text/event-stream",
          "Content-Location": `/threads/${thread.thread_id}/runs/${runId}`,
        },
      },
    );
  };

  const route = (method: string, path: string, body: unknown): Response => {
    const record = isRecord(body) ? body : {};
    if (method === "GET" && (path === "/ok" || path === "/info" || path === "/health")) {
      return json({ ok: true });
    }
    if (method === "POST" && path === "/threads") {
      const id = typeof record.thread_id === "string" ? record.thread_id : crypto.randomUUID();
      const existing = threads.get(id);
      if (existing) return json(threadJson(existing));
      const metadata = isRecord(record.metadata) ? record.metadata : {};
      return json(threadJson(seed({ id, metadata: { thread_name: "", ...metadata } })));
    }
    if (method === "POST" && path === "/threads/search") {
      const filter = isRecord(record.metadata) ? record.metadata : {};
      const ids = Array.isArray(record.ids) ? record.ids : null;
      const field = record.sort_by === "updated_at" ? "updated_at" : "created_at";
      const ascending = record.sort_by ? record.sort_order === "asc" : false;
      const offset = typeof record.offset === "number" ? record.offset : 0;
      const limit = typeof record.limit === "number" ? record.limit : 10;
      const found = [...threads.values()]
        .filter((thread) =>
          Object.entries(filter).every(([key, value]) => thread.metadata[key] === value),
        )
        .filter((thread) => !ids || ids.includes(thread.thread_id))
        .filter((thread) => !record.status || threadJson(thread).status === record.status)
        .sort((a, b) => (a[field] < b[field] ? -1 : 1) * (ascending ? 1 : -1));
      return json(found.slice(offset, offset + limit).map(threadJson));
    }
    const match = /^\/threads\/([^/]+)(\/.*)?$/.exec(path);
    const thread = match?.[1] ? threads.get(decodeURIComponent(match[1])) : undefined;
    if (!match || !thread) return json({ detail: "Thread not found" }, 404);
    const rest = match[2] ?? "";
    if (rest === "" && method === "GET") return json(threadJson(thread));
    if (rest === "" && method === "PATCH") {
      if (isRecord(record.metadata)) Object.assign(thread.metadata, record.metadata);
      thread.updated_at = now();
      return json(threadJson(thread));
    }
    if (rest === "" && method === "DELETE") {
      threads.delete(thread.thread_id);
      return new Response(null, { status: 204 });
    }
    if (rest === "/state" && method === "GET") return json(stateJson(thread));
    if (rest === "/history" && method === "POST") return json([stateJson(thread)]);
    if (rest === "/runs/stream" && method === "POST") {
      if (nextRunError !== null) {
        const message = nextRunError;
        nextRunError = null;
        const events = [
          ["metadata", { run_id: nextId("run"), attempt: 1 }],
          ["error", { error: "ValueError", message }],
          ["end", { status: "error" }],
        ];
        return new Response(
          events
            .map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
            .join(""),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }
      if (nextRunFailure !== null) {
        const status = nextRunFailure;
        nextRunFailure = null;
        return json({ detail: "Run refused" }, status);
      }
      runs.push({ threadId: thread.thread_id, body: record });
      return stream(thread, record);
    }
    return json({ detail: "Not Found" }, 404);
  };

  const fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const request = new Request(
      input instanceof Request ? input : new URL(String(input), window.location.href),
      init,
    );
    const url = new URL(request.url);
    const path = url.pathname.slice(base.length) || "/";
    const raw = await request.text();
    const body: unknown = raw ? JSON.parse(raw) : undefined;
    requests.push({ method: request.method, path, body });
    const hold = holds.find((entry) => entry.method === request.method && entry.path === path);
    if (hold) {
      // A held request still ends when its caller aborts it, as a real fetch does.
      const signal = init?.signal ?? request.signal;
      await Promise.race([
        hold.held,
        new Promise<never>((_, reject) => {
          if (signal.aborted) reject(signal.reason);
          signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        }),
      ]);
    }
    if (failure === "network") throw new TypeError("Failed to fetch");
    if (failure !== null) return new Response("Bad Gateway", { status: failure });
    return route(request.method, path, body);
  };

  return {
    fetch,
    threads,
    /** Every request, in order, with its path below the Aegra prefix. */
    requests,
    /** Every run started, with its request body. */
    runs,
    seed,
    fail: (next: AegraFailure | null) => {
      failure = next;
    },
    /** Answers the next run request with `status` and leaves the thread unchanged. */
    failNextRun: (status: number) => {
      nextRunFailure = status;
    },
    /** Streams a graph `error` for the next run before it does anything; the thread is unchanged. */
    errorNextRun: (message: string) => {
      nextRunError = message;
    },
    /** Keeps requests to `path` unanswered until the returned function runs. */
    hold: (method: string, path: string): (() => void) => {
      let release = () => {};
      const held = new Promise<void>((resolve) => {
        release = resolve;
      });
      holds.push({ method, path, held });
      return release;
    },
    /** Pauses the next run after its first text chunk until the returned function runs. */
    pauseNextStream: (): (() => void) => {
      let release = () => {};
      streamGate = new Promise<void>((resolve) => {
        release = resolve;
      });
      return release;
    },
    /** Requests whose path matches, such as `/threads/abc/state`. */
    requestsTo: (method: string, path: string | RegExp) =>
      requests.filter(
        (request) =>
          request.method === method &&
          (typeof path === "string" ? request.path === path : path.test(request.path)),
      ),
  };
}

export type FakeAegra = ReturnType<typeof createFakeAegra>;

/** Routes `/agent/*` to `aegra`, the rest to an installed test fake; never to a network. */
export function installFakeAegra(aegra: FakeAegra, base = "/agent") {
  const installed = globalThis.fetch;
  const fallback: typeof fetch = vi.isMockFunction(installed)
    ? installed
    : async (input) => {
        throw new Error(`Unexpected request outside Aegra in a test: ${String(input)}`);
      };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(
        input instanceof Request ? input.url : String(input),
        window.location.href,
      );
      return url.pathname === base || url.pathname.startsWith(`${base}/`)
        ? aegra.fetch(input, init)
        : fallback(input, init);
    }),
  );
}

export const human = (content: string, id = `human-${content}`) => ({
  id,
  type: "human",
  content,
});

export const ai = (content: string, id = `resp-${content}`) => ({
  id,
  type: "ai",
  content,
  tool_calls: [],
});
