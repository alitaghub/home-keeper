// The agent panel: chat with the agent behind the web container's /agent route.
import {
  AssistantRuntimeProvider,
  AuiConfig,
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  type ToolCallMessagePartProps,
  Tools,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import {
  type LangChainMessage,
  type LangGraphStreamCallback,
  type UseLangGraphRuntimeOptions,
  useLangGraphRuntime,
} from "@assistant-ui/react-langgraph";
import { Client } from "@langchain/langgraph-sdk";
import { History, Maximize2, Minimize2, Plus, Trash2, X } from "lucide-react";
import { lazy, type ReactNode, Suspense, useContext, useEffect, useRef, useState } from "react";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import { Button } from "@/components/ui/button";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ApprovalScope, RunsContext, ToolCall } from "./Approvals";
import { ChartBoundary, ChartSkeleton } from "./ChartStates";
import { panelSlot, type WidthControl } from "./layout";
import { parseMessages } from "./messages";
import { DeviceNames } from "./names";
import { createRunEvents, questionOf, type RunEvents } from "./runs";
import { adaptAegraStream } from "./stream";
import { aegraThreadList, ChatNotFound } from "./threads";

const ASSISTANT_ID = "agent";
// The chart code, and Recharts with it, loads with the first chart a chat shows.
const ChartToolUI = lazy(() =>
  import("./ChartToolUI").then((module) => ({ default: module.ChartToolUI })),
);
// A chart whose code fails to load, or that fails to draw, shows its own error card.
function ChartTool(props: ToolCallMessagePartProps) {
  return (
    <ChartBoundary>
      <Suspense fallback={<ChartSkeleton label="Drawing a chart" />}>
        <ChartToolUI {...props} />
      </Suspense>
    </ChartBoundary>
  );
}
// The agent's show_chart calls draw as charts, outside the collapsed tool group. The tool
// runs on the server; this only registers its renderer. Every other tool keeps ToolCall.
const CHART_TOOLS = AuiConfig({
  tools: Tools({
    toolkit: { show_chart: { type: "backend", display: "standalone", render: ChartTool } },
  }),
});
// The docs are not in the image, so the panel links to the hosted page.
const agentDocs = "https://github.com/alitaghub/home-keeper/blob/main/docs/agent.md";
// The chat that was open, so a reload reopens it.
const THREAD_KEY = "home-keeper.agent-thread";

// Browser storage can be missing or blocked; the chat then simply starts empty.
function readThread(): string | null {
  try {
    return localStorage.getItem(THREAD_KEY);
  } catch {
    return null;
  }
}

function saveThread(threadId: string | undefined) {
  try {
    if (threadId) localStorage.setItem(THREAD_KEY, threadId);
    else localStorage.removeItem(THREAD_KEY);
  } catch {
    // Not remembered; nothing else depends on it.
  }
}

// What the panel shows for a run that fails, instead of the SDK's own error text.
const RUN_FAILED =
  "The agent could not finish its answer. Check that it is running, then ask again.";

type Agent = Required<Pick<UseLangGraphRuntimeOptions, "load" | "stream">> & {
  threadList: ReturnType<typeof aegraThreadList>;
  runs: RunEvents;
};

// The SDK client lives per panel: it reads `fetch` when it is made.
function createAgent(): Agent {
  const client = new Client({ apiUrl: `${window.location.origin}/agent` });

  const runs = createRunEvents();

  const stream: LangGraphStreamCallback<LangChainMessage> = async function* (messages, config) {
    // The approval decisions travel JSON-encoded (see ApprovalScope).
    const resume: unknown = config.command ? JSON.parse(config.command.resume) : undefined;
    let threadId: string | undefined;
    let received = false;
    let failed = false;
    try {
      ({ externalId: threadId } = await config.initialize());
      runs.emit({ type: "start", threadId });
      if (!threadId) throw new Error("The chat has no Aegra thread.");
      const events = client.runs.stream(threadId, ASSISTANT_ID, {
        input: messages.length ? { messages } : null,
        streamMode: ["messages-tuple", "updates"],
        signal: config.abortSignal,
        onDisconnect: "cancel",
        ...(resume !== undefined && { command: { resume } }),
      });
      for await (const event of adaptAegraStream(events)) {
        received = true;
        // Aegra reports a failed run inside the stream, too.
        if (event.event === "error") failed = true;
        yield event;
      }
    } catch (error) {
      if (config.abortSignal.aborted) throw error;
      failed = true;
      throw new Error(RUN_FAILED, { cause: error });
    } finally {
      if (!config.abortSignal.aborted) {
        runs.emit({
          type: "end",
          threadId,
          resume: resume !== undefined,
          failed,
          // Without any answer the runtime drops the question, so the panel gives it back.
          unsent: failed && !received && resume === undefined ? questionOf(messages) : undefined,
        });
      }
    }
  };

  const load: Agent["load"] = async (threadId) => {
    try {
      const state = await client.threads.getState<unknown>(threadId);
      return {
        messages: parseMessages(state.values),
        // A chat left waiting for approval shows its cards again.
        interrupts: state.tasks.flatMap((task) =>
          task.interrupts.map(({ value }) => ({ value, resumable: true })),
        ),
      };
    } catch (error) {
      // The runtime only logs a failed load and shows the chat as empty.
      runs.emit({ type: "load-failed", threadId });
      throw error;
    }
  };

  return { threadList: aegraThreadList(client), load, stream, runs };
}

type Reachability = "checking" | "up" | "down";

const HEALTH_TIMEOUT_MS = 5000;

export default function AgentPanel({
  names,
  onClose,
  width,
}: {
  names: ReadonlyMap<string, string>;
  onClose: () => void;
  /** Absent on a phone, where the panel is a full-screen sheet. */
  width?: WidthControl | undefined;
}) {
  const [reachable, setReachable] = useState<Reachability>("checking");
  const [attempt, setAttempt] = useState(0);
  // A fresh chat runtime, which reopens the saved chat.
  const [session, setSession] = useState(0);

  // Without the agent profile, the web container answers /agent with an error.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `attempt` re-runs the check on "Try again"
  useEffect(() => {
    const controller = new AbortController();
    setReachable("checking");
    // A stalled agent counts as down; an unmount cancels without a verdict.
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(HEALTH_TIMEOUT_MS)]);
    fetch(`${window.location.origin}/agent/health`, { signal }).then(
      (response) => setReachable(response.ok ? "up" : "down"),
      () => controller.signal.aborted || setReachable("down"),
    );
    return () => controller.abort();
  }, [attempt]);

  const widthLabel = width?.expanded ? "Restore agent panel width" : "Expand agent panel";
  const controls = (
    <>
      {width && (
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={widthLabel}
          title={widthLabel}
          onClick={width.toggle}
        >
          {width.expanded ? <Minimize2 aria-hidden="true" /> : <Maximize2 aria-hidden="true" />}
        </Button>
      )}
      <Button variant="ghost" size="icon-sm" aria-label="Close agent panel" onClick={onClose}>
        <X aria-hidden="true" />
      </Button>
    </>
  );

  return (
    <section id="agent-panel" aria-label="Agent" className={panelSlot}>
      {reachable === "up" ? (
        <AgentChat
          key={session}
          names={names}
          controls={controls}
          onReload={() => setSession((n) => n + 1)}
        />
      ) : (
        <>
          <PanelHeader>{controls}</PanelHeader>
          <div className="grid content-start gap-3 p-4 text-sm" role="status" aria-live="polite">
            {reachable === "checking" ? (
              <p className="text-muted-foreground">Connecting to the agent…</p>
            ) : (
              <>
                <p>
                  The agent isn't running. Start it with{" "}
                  <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                    --profile agent
                  </code>
                  .
                </p>
                <p className="flex flex-wrap items-center gap-3">
                  <a
                    href={agentDocs}
                    target="_blank"
                    rel="noreferrer"
                    className="font-medium underline underline-offset-4"
                  >
                    How to start the agent
                  </a>
                  <Button variant="outline" size="sm" onClick={() => setAttempt((n) => n + 1)}>
                    Try again
                  </Button>
                </p>
              </>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function PanelHeader({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-1 border-b px-3 py-2">
      <h2 className="mr-auto text-sm font-semibold">Agent</h2>
      {children}
    </div>
  );
}

function AgentChat({
  names,
  controls,
  onReload,
}: {
  names: ReadonlyMap<string, string>;
  controls: ReactNode;
  onReload: () => void;
}) {
  const [agent] = useState(createAgent);
  // Read once, before the runtime reports any thread change.
  const [savedThread] = useState(readThread);
  const [recentOpen, setRecentOpen] = useState(false);
  // Threads restore through the thread-list adapter (see RestoreThread).
  const runtime = useLangGraphRuntime({
    unstable_threadListAdapter: agent.threadList,
    onThreadIdChange: saveThread,
    load: agent.load,
    stream: agent.stream,
    unstable_allowCancellation: true,
  });

  return (
    <AssistantRuntimeProvider runtime={runtime} config={CHART_TOOLS}>
      <DeviceNames.Provider value={names}>
        <TooltipProvider>
          <RestoreThread threadId={savedThread} threadList={agent.threadList} />
          <RunsContext.Provider value={agent.runs}>
            <PanelHeader>
              <ThreadListPrimitive.New asChild>
                <Button variant="ghost" size="icon-sm" aria-label="New chat" title="New chat">
                  <Plus aria-hidden="true" />
                </Button>
              </ThreadListPrimitive.New>
              <Button
                variant={recentOpen ? "secondary" : "ghost"}
                size="icon-sm"
                aria-label="Recent chats"
                title="Recent chats"
                aria-expanded={recentOpen}
                aria-controls="agent-recent"
                onClick={() => setRecentOpen((open) => !open)}
              >
                <History aria-hidden="true" />
              </Button>
              <DeleteChat />
              {controls}
            </PanelHeader>
            {recentOpen && <RecentChats onPick={() => setRecentOpen(false)} />}
            {/* Hidden, not unmounted, so a half-decided approval keeps its decisions. */}
            <div hidden={recentOpen} className="flex min-h-0 flex-1 flex-col">
              <RunFailure />
              <OpenChat names={names} onReload={onReload} />
            </div>
          </RunsContext.Provider>
        </TooltipProvider>
      </DeviceNames.Provider>
    </AssistantRuntimeProvider>
  );
}

// A chat's id, or undefined for a chat Aegra has not created yet.
type ChatId = string | undefined;

/** Shows why a chat's last question failed, gives an unsent question back in that chat, and
 * keeps Recent's activity order current after every run. A run goes on after you switch
 * chats, so each failure stays with its own chat. */
function RunFailure() {
  const aui = useAui();
  const runs = useContext(RunsContext);
  const threadId = useAuiState((s) => s.threadListItem.externalId);
  // Each failed chat, with the question still to give back.
  const [failures, setFailures] = useState<ReadonlyMap<ChatId, string | undefined>>(new Map());
  useEffect(
    () =>
      runs?.subscribe((event) => {
        if (event.type === "load-failed") return;
        if (event.type === "start") {
          // A failure before Aegra made the chat belongs to whichever new chat runs next.
          return setFailures((all) => without(all, event.threadId, undefined));
        }
        void aui
          .threads()
          .reload()
          .catch(() => {});
        // A failed resume shows its error on the approval cards instead.
        if (!event.failed || event.resume) return;
        setFailures((all) => new Map(all).set(event.threadId, event.unsent));
      }),
    [aui, runs],
  );
  const unsent = failures.get(threadId);
  useEffect(() => {
    if (unsent === undefined) return;
    const composer = aui.composer();
    if (!composer.getState().text) composer.setText(unsent);
    setFailures((all) => new Map(all).set(threadId, undefined));
  }, [aui, threadId, unsent]);
  if (!failures.has(threadId)) return null;
  return (
    <p
      role="alert"
      className="mx-3 mt-3 rounded-md border border-destructive/50 px-3 py-2 text-sm text-destructive"
    >
      {RUN_FAILED}
    </p>
  );
}

function without<K, V>(map: ReadonlyMap<K, V>, ...keys: K[]): ReadonlyMap<K, V> {
  const rest = new Map(map);
  for (const key of keys) rest.delete(key);
  return rest;
}

function RestoreThread({
  threadId,
  threadList,
}: {
  threadId: string | null;
  threadList: Agent["threadList"];
}) {
  const aui = useAui();
  // StrictMode runs effects twice; the chat must load once.
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || !threadId) return;
    restored.current = true;
    // A chat deleted elsewhere is forgotten quietly; the panel stays on a new chat.
    threadList.fetch(threadId).then(
      () => aui.threads().switchToThread(threadId),
      (error: unknown) =>
        error instanceof ChatNotFound
          ? saveThread(undefined)
          : aui.threads().switchToThread(threadId),
    );
  }, [aui, threadId, threadList]);
  return null;
}

function OpenChat({
  names,
  onReload,
}: {
  names: ReadonlyMap<string, string>;
  onReload: () => void;
}) {
  const threadId = useAuiState((s) => s.threads.mainThreadId);
  const externalId = useAuiState((s) => s.threadListItem.externalId);
  const runs = useContext(RunsContext);
  const [unloaded, setUnloaded] = useState<string | null>(null);
  useEffect(
    () =>
      runs?.subscribe((event) => {
        if (event.type === "load-failed") setUnloaded(event.threadId);
      }),
    [runs],
  );
  // Without its history, the chat takes no question: the agent would answer from turns
  // you cannot see.
  if (unloaded !== null && unloaded === externalId) {
    return (
      <div className="grid content-start gap-3 p-4 text-sm" role="alert">
        <p>This chat could not be loaded.</p>
        <p>
          <Button variant="outline" size="sm" onClick={onReload}>
            Try again
          </Button>
        </p>
      </div>
    );
  }
  return (
    // Keyed by the chat, so an approval never shows in another chat.
    <ApprovalScope key={threadId} names={names}>
      <div className="min-h-0 flex-1">
        <Thread components={{ ToolFallback: ToolCall, Welcome }} />
      </div>
    </ApprovalScope>
  );
}

function Welcome() {
  return (
    <div className="mb-6 grid gap-1 px-2">
      <p className="text-lg font-medium tracking-tight">Ask about your home</p>
      <p className="text-sm text-muted-foreground">
        How long a light was on, what changed today, or which devices are monitored.
      </p>
    </div>
  );
}

function DeleteChat() {
  const aui = useAui();
  const saved = useAuiState((s) => s.threadListItem.remoteId !== undefined);
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      aria-label="Delete chat"
      title="Delete chat"
      disabled={!saved}
      onClick={() => aui.threadListItem().delete()}
    >
      <Trash2 aria-hidden="true" />
    </Button>
  );
}

function RecentChats({ onPick }: { onPick: () => void }) {
  const loading = useAuiState((s) => s.threads.isLoading);
  const empty = useAuiState((s) => s.threads.threadIds.length === 0);
  const hasMore = useAuiState((s) => s.threads.hasMore);
  const loadingMore = useAuiState((s) => s.threads.isLoadingMore);
  const aui = useAui();
  return (
    <nav id="agent-recent" aria-label="Recent chats" className="min-h-0 flex-1 overflow-y-auto p-2">
      {loading ? (
        <p className="p-2 text-sm text-muted-foreground">Loading chats…</p>
      ) : empty ? (
        <p className="p-2 text-sm text-muted-foreground">No chats yet.</p>
      ) : (
        <ThreadListPrimitive.Root className="grid gap-0.5">
          <ThreadListPrimitive.Items>
            {() => (
              <ThreadListItemPrimitive.Trigger
                onClick={onPick}
                className="w-full truncate rounded-md px-2.5 py-2 text-left text-sm hover:bg-muted focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none data-active:bg-muted"
              >
                <ThreadListItemPrimitive.Title fallback="New chat" />
              </ThreadListItemPrimitive.Trigger>
            )}
          </ThreadListPrimitive.Items>
          {hasMore && (
            <Button
              variant="ghost"
              size="sm"
              className="justify-self-start"
              disabled={loadingMore}
              onClick={() => void aui.threads().loadMore()}
            >
              {loadingMore ? "Loading more chats…" : "Load more chats"}
            </Button>
          )}
        </ThreadListPrimitive.Root>
      )}
    </nav>
  );
}
