import { House, MessageSquare, RefreshCw, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { type LayoutStorage, useDefaultLayout, usePanelRef } from "react-resizable-panels";
import { AGENT_MIN_PX, AGENT_WIDTH } from "./agent/layout";
import {
  api,
  type CommandRecord,
  errorMessage,
  mutationHeaders,
  type Schemas,
  unwrap,
} from "./api/client";
import { AgentSlot } from "./components/AgentSlot";
import { Catalog, deviceName, lightName } from "./components/Catalog";
import { Commands } from "./components/Commands";
import { HistoryDialog } from "./components/History";
import { MeasurementDialog } from "./components/Measurements";
import { type DeviceCommand, Monitored } from "./components/Monitored";
import { Timeline } from "./components/Timeline";
import { Alert, AlertDescription, AlertTitle } from "./components/ui/alert";
import { Badge } from "./components/ui/badge";
import { Button } from "./components/ui/button";
import { Card, CardContent, CardHeader } from "./components/ui/card";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "./components/ui/resizable";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
import { date, time } from "./format";
import { cn } from "./lib/utils";
import { useAgentPanel } from "./useAgentPanel";
import { useRecentMeasurements } from "./useMeasurements";
import { useCommands, useTimeline } from "./useTimeline";
import { useRecentUsage } from "./useUsage";
import { useWideScreen } from "./useWideScreen";

const POLL_MS = 3000;

const TABS = [
  { id: "home", label: "Home" },
  { id: "history", label: "History" },
  { id: "setup", label: "Setup" },
] as const;

type Tab = (typeof TABS)[number]["id"];

const tabFromHash = (): Tab =>
  TABS.find((tab) => `#${tab.id}` === window.location.hash)?.id ?? "home";

// How far back History reaches, in the hosted docs: the image does not ship docs/.
const recorderDocs =
  "https://github.com/alitaghub/home-keeper/blob/main/docs/home-assistant.md#history-retention";

// Why Home Assistant cannot be read, from /api/health. Fixed text, never upstream text.
const unreachableMessages: Record<NonNullable<Schemas["HealthStatus"]["error"]>, string> = {
  authentication: "Home Assistant rejected the reader's token. Check the token and its user.",
  connection: "Home Assistant does not answer. Check its address and container networking.",
  tls: "The certificate of Home Assistant could not be verified. Configure a trusted certificate.",
  timeout: "Home Assistant did not answer in time.",
  invalid_data: "Home Assistant sent data Home Keeper cannot read.",
  missing: "Home Assistant no longer has a device Home Keeper asked for.",
  discovery: "Home Assistant refused to list its devices.",
  configuration: "The reader's Home Assistant address or token file is invalid.",
  reader_unreachable: "The reader service does not answer.",
};

// The API answers a command with a final outcome; `pending` and `interrupted`
// appear only in the command history, and read as "may not have changed" here.
const outcomeMessages: Record<
  CommandRecord["outcome"],
  (name: string, command: DeviceCommand) => string
> = {
  observed: (name, command) =>
    command.action === "on" && command.brightness_pct != null
      ? `${name} is now on at ${command.brightness_pct}%.`
      : `${name} is now ${command.action}.`,
  not_observed: (name) => `Sent to ${name}, but no change was seen yet.`,
  refused: (name) => `Home Assistant refused the command for ${name}.`,
  unreachable: (name) => `Cannot reach the controller. ${name} may not have changed.`,
  pending: (name) => `Sent to ${name}, but no change was seen yet.`,
  interrupted: (name) => `The command for ${name} was interrupted. ${name} may not have changed.`,
};

// How long a command's result stays on screen, unless another command replaces it.
const COMMAND_NOTICE_MS = 15000;

type CommandResult = { notice: string; reload: boolean };

// Matches `unwrap`, so a dropped connection reads like any other failed request.
const commandFailed: CommandResult = { notice: "Request failed. Please retry.", reload: false };

async function sendDeviceCommand(
  entity: Schemas["MonitoredEntity"],
  command: DeviceCommand,
): Promise<CommandResult> {
  const name = lightName(entity);
  const { data, error, response } = await api.POST("/api/devices/{entity_id}/command", {
    params: { path: { entity_id: entity.id } },
    body: { command, sender: { kind: "dashboard" } },
    headers: mutationHeaders,
  });
  if (response.ok && data) {
    return { notice: outcomeMessages[data.outcome](name, command), reload: true };
  }
  // A 409 carries CommandRefused; a 422 carries a list of validation errors.
  const detail = response.status === 409 ? error?.detail : undefined;
  return detail && !Array.isArray(detail)
    ? { notice: `${name}: ${detail.message}`, reload: true }
    : commandFailed;
}

type Snapshot = {
  // Null while Home Assistant cannot be read: an earlier catalog is never shown as current.
  catalog: Schemas["CatalogResponse"] | null;
  selection: Schemas["Selection"];
  monitored: Schemas["MonitoredEntities"];
  health: Schemas["HealthStatus"];
};

// Polls overlap when a response is slow. Each load takes a number, and a result
// is applied only if no later load has applied its own.
type LoadOrder = { started: number; snapshot: number; control: number };

// The panel's width, per browser. Storage can be blocked; the panel then opens at its default.
const layoutStorage: LayoutStorage = {
  getItem: (key) => {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  setItem: (key, value) => {
    try {
      localStorage.setItem(key, value);
    } catch {
      // Not remembered.
    }
  },
};

export function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  // Set when the latest poll failed, so the snapshot's states are no longer current.
  const [stale, setStale] = useState(false);
  // Apart from the snapshot, so a failed or off control read hides the toggles
  // even when another endpoint fails in the same poll.
  const [control, setControl] = useState<Schemas["ControlStatus"]["state"]>("off");
  const order = useRef<LoadOrder>({ started: 0, snapshot: 0, control: 0 });
  const [message, setMessage] = useState("");
  // Command results live apart from `message`, which every poll rewrites.
  const [commandNotice, setCommandNotice] = useState("");
  const noticeTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const timeline = useTimeline();
  const { load: loadTimeline, restart: restartTimeline, showingOlder } = timeline;
  const commands = useCommands();
  const { load: loadCommands, showingOlder: showingOlderCommands } = commands;
  // One selection change at a time.
  const busy = useRef(false);
  // A command takes several seconds (the send, then up to 5 s of waiting for the
  // change), so commands lock per device, apart from `busy`.
  const sending = useRef(new Set<string>());
  const [sendingIds, setSendingIds] = useState<ReadonlySet<string>>(new Set());
  const [historyFor, setHistoryFor] = useState<Schemas["MonitoredEntity"] | null>(null);
  const [agentOpen, setAgentOpen] = useAgentPanel();
  const askButton = useRef<HTMLButtonElement>(null);
  const closeAgent = () => {
    setAgentOpen(false);
    // On a phone the sheet covered the page; focus returns to where it was opened.
    askButton.current?.focus();
  };
  const wide = useWideScreen();
  const savedLayout = useDefaultLayout({
    id: "home-keeper.agent-layout",
    panelIds: agentOpen ? ["dashboard", "agent"] : ["dashboard"],
    storage: layoutStorage,
  });
  const agentPanel = usePanelRef();
  const [agentShare, setAgentShare] = useState<number>(AGENT_WIDTH.initial);
  // The width an expanded panel goes back to.
  const restoreShare = useRef<number>(AGENT_WIDTH.initial);
  const expanded = agentShare >= AGENT_WIDTH.max - 0.5;
  const toggleExpanded = () => {
    const panel = agentPanel.current;
    if (!panel) return;
    if (expanded) return panel.resize(`${restoreShare.current}%`);
    restoreShare.current = panel.getSize().asPercentage;
    panel.resize(`${AGENT_WIDTH.max}%`);
  };

  const load = useCallback(async () => {
    const seq = ++order.current.started;
    const isLatest = (kind: "snapshot" | "control") => {
      if (seq < order.current[kind]) return false;
      order.current[kind] = seq;
      return true;
    };
    // A failed control read counts as off.
    void unwrap(api.GET("/api/control")).then(
      ({ state }) => isLatest("control") && setControl(state),
      () => isLatest("control") && setControl("off"),
    );
    try {
      const [catalog, selection, monitored, health] = await Promise.all([
        // The catalog answers 503 while Home Assistant cannot be read; the rest still load.
        unwrap(api.GET("/api/catalog")).catch(() => null),
        unwrap(api.GET("/api/selection")),
        unwrap(api.GET("/api/monitored")),
        unwrap(api.GET("/api/health")),
      ]);
      if (!isLatest("snapshot")) return;
      setSnapshot({ catalog, selection, monitored, health });
      setStale(false);
      setMessage("");
      await Promise.all([
        showingOlder.current ? null : loadTimeline(),
        showingOlderCommands.current ? null : loadCommands(),
      ]);
    } catch {
      if (!isLatest("snapshot")) return;
      setStale(true);
      setMessage("Cannot reach Home Keeper. States are unknown until the connection returns.");
    }
  }, [loadTimeline, showingOlder, loadCommands, showingOlderCommands]);

  useEffect(() => () => clearTimeout(noticeTimer.current), []);

  useEffect(() => {
    const follow = () => setTab(tabFromHash());
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, []);

  const openTab = (next: Tab) => {
    setTab(next);
    window.history.replaceState(null, "", `#${next}`);
  };

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const mutate = async (send: () => Promise<void>) => {
    if (busy.current) return;
    busy.current = true;
    try {
      await send();
      await load();
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      busy.current = false;
    }
  };

  const toggle = (id: string, selected: boolean) =>
    mutate(async () => {
      await unwrap(
        api.PUT("/api/selection/{entity_id}", {
          params: { path: { entity_id: id } },
          body: { selected },
          headers: mutationHeaders,
        }),
      );
      // History lists only monitored devices, so the pages loaded or in flight are stale.
      restartTimeline(selected ? null : id);
    });

  const showCommandNotice = (notice: string) => {
    clearTimeout(noticeTimer.current);
    setCommandNotice(notice);
    if (notice) noticeTimer.current = setTimeout(() => setCommandNotice(""), COMMAND_NOTICE_MS);
  };

  const sendCommand = async (entity: Schemas["MonitoredEntity"], command: DeviceCommand) => {
    if (sending.current.has(entity.id)) return;
    sending.current.add(entity.id);
    setSendingIds(new Set(sending.current));
    showCommandNotice("");
    let result: CommandResult;
    try {
      result = await sendDeviceCommand(entity, command);
    } catch {
      result = commandFailed;
    } finally {
      sending.current.delete(entity.id);
      setSendingIds(new Set(sending.current));
    }
    showCommandNotice(result.notice);
    // The lock is already released: a slow reload must not hold the button.
    if (result.reload) void load();
  };

  const health = snapshot?.health;
  const live = (snapshot?.catalog ?? health)?.source_id === "home_assistant";
  const unreachable = health?.status === "unreachable";
  const controlOn = snapshot !== null && control === "on";
  // The poll reads its endpoints at once, so states read just before an outage are
  // dropped too: nothing shows a state, or offers a switch, that may not be current.
  const statesKnown = !stale && !unreachable;
  const monitoredEntities =
    snapshot?.monitored.entities.map((entity) =>
      statesKnown ? entity : { ...entity, state: null },
    ) ?? null;
  const names = new Map(
    [...(snapshot?.catalog?.entities ?? []), ...(snapshot?.monitored.entities ?? [])].map(
      (entity) => [entity.id, lightName(entity)],
    ),
  );

  // The strips reload when what the cards show changes: a selection, a state or a brightness.
  // A measurement changes its state often and has no strip, so it is left out.
  const usage = useRecentUsage(
    snapshot &&
      JSON.stringify(
        snapshot.monitored.entities
          .filter(({ device_kind }) => device_kind !== "measurement")
          .map(({ id, state }) => [
            id,
            state?.state,
            state?.device_kind === "light" ? state.brightness : null,
          ]),
      ),
  );
  // The sparklines reload each minute, and when the monitored measurements change. Without
  // one, nothing loads.
  const measuredIds = (snapshot?.monitored.entities ?? [])
    .filter(({ device_kind }) => device_kind === "measurement")
    .map(({ id }) => id);
  const measurements = useRecentMeasurements(
    measuredIds.length ? JSON.stringify(measuredIds) : null,
  );

  const panel = (id: Tab) => ({
    value: id,
    id: `panel-${id}`,
    "aria-labelledby": `tab-${id}`,
    // Every panel stays mounted, so a hidden tab keeps its state and its ids.
    forceMount: true as const,
    hidden: tab !== id,
    className: "grid gap-6",
  });

  const dashboard = (
    <>
      <header className="sticky top-0 z-10 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <div className="flex items-center gap-2 font-semibold tracking-tight">
            <House className="size-5" aria-hidden="true" />
            <strong>Home Keeper</strong>
          </div>
          <TabsList aria-label="Views" className="order-last w-full sm:order-none sm:w-auto">
            {TABS.map(({ id, label }) => (
              <TabsTrigger
                key={id}
                id={`tab-${id}`}
                value={id}
                aria-controls={`panel-${id}`}
                // Radix switches tabs on mouse down; a click (keyboard, tests) switches too.
                onClick={() => openTab(id)}
                className="sm:px-4"
              >
                {label}
              </TabsTrigger>
            ))}
          </TabsList>
          <Badge id="source" variant="outline" className="ml-auto font-mono tracking-wide">
            {snapshot ? (live ? "HOME ASSISTANT" : "SYNTHETIC DEMO") : "CONNECTING"}
          </Badge>
          <Button
            ref={askButton}
            id="ask"
            type="button"
            variant={agentOpen ? "secondary" : "default"}
            size="sm"
            aria-expanded={agentOpen}
            aria-controls={agentOpen ? "agent-panel" : undefined}
            onClick={() => (agentOpen ? closeAgent() : setAgentOpen(true))}
          >
            <MessageSquare aria-hidden="true" />
            Ask
          </Button>
        </div>
      </header>
      <main className="mx-auto grid w-full max-w-5xl gap-4 px-4 py-6">
        <p id="message" className="text-sm text-muted-foreground" role="status" aria-live="polite">
          {[commandNotice, message].filter(Boolean).join(" ")}
        </p>
        {unreachable && (
          <Alert id="unreachable" variant="destructive">
            <TriangleAlert aria-hidden="true" />
            <AlertTitle>Can't reach Home Assistant</AlertTitle>
            {health.error && (
              <AlertDescription>{unreachableMessages[health.error]}</AlertDescription>
            )}
          </Alert>
        )}

        <TabsContent {...panel("home")}>
          <p
            id="status-line"
            className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground"
          >
            <span
              className={cn(
                "size-2 rounded-full",
                health?.status === "healthy" && "bg-foreground",
                health?.status === "unreachable" && "bg-danger",
                !health && "bg-muted-foreground",
              )}
              aria-hidden="true"
            />
            {health
              ? `Home Assistant ${health.status} · checked ${time(health.checked_at)}`
              : "Connecting…"}
            <Badge variant={controlOn ? "default" : "secondary"} className="ml-auto">
              {controlOn ? "CONTROL ON" : "READ ONLY"}
            </Badge>
          </p>
          <Monitored
            entities={monitoredEntities}
            usage={usage}
            measurements={measurements}
            onHistory={setHistoryFor}
            onStop={(id) => void toggle(id, false)}
            onSwitch={
              controlOn && statesKnown
                ? (entity, command) => void sendCommand(entity, command)
                : null
            }
            onChooseLights={() => openTab("setup")}
            sending={sendingIds}
          />
        </TabsContent>

        <TabsContent {...panel("history")}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 id="timeline-heading" className="text-2xl font-semibold tracking-tight">
              History
            </h2>
            <Button
              id="timeline-refresh"
              type="button"
              variant="outline"
              size="sm"
              disabled={timeline.loading}
              onClick={() => void Promise.all([loadTimeline(), loadCommands()])}
            >
              <RefreshCw aria-hidden="true" />
              Refresh history
            </Button>
          </div>
          {(controlOn || Boolean(commands.commands?.length)) && (
            <section aria-labelledby="commands-heading">
              <Card className="gap-4 py-5">
                <CardHeader className="px-5">
                  <h3 id="commands-heading" className="leading-none font-semibold">
                    Device commands
                  </h3>
                </CardHeader>
                <CardContent className="px-5">
                  <Commands
                    commands={commands.commands}
                    names={names}
                    hasMore={commands.hasMore}
                    loading={commands.loading}
                    onMore={() => void loadCommands(true)}
                  />
                </CardContent>
              </Card>
            </section>
          )}
          <section aria-labelledby="changes-heading">
            <Card className="gap-4 py-5">
              <CardHeader className="gap-2 px-5">
                <h3 id="changes-heading" className="leading-none font-semibold">
                  Recorded changes
                </h3>
                <p
                  id="timeline-status"
                  className={cn(
                    "text-sm",
                    timeline.failed ? "text-danger" : "text-muted-foreground",
                  )}
                  role="status"
                  aria-live="polite"
                >
                  {timeline.failed ? (
                    "Cannot load recorded history. Previously displayed entries may be out of date."
                  ) : (
                    <>
                      History comes from Home Assistant. How far back it goes depends on its{" "}
                      <a
                        href={recorderDocs}
                        target="_blank"
                        rel="noreferrer"
                        className="font-medium text-foreground underline underline-offset-4"
                      >
                        recorder settings
                      </a>
                      .
                    </>
                  )}
                </p>
              </CardHeader>
              <CardContent className="grid gap-4 px-5">
                <Timeline events={timeline.events} names={names} />
                <Button
                  id="timeline-more"
                  type="button"
                  variant="outline"
                  size="sm"
                  className="justify-self-start"
                  hidden={!timeline.hasMore}
                  disabled={timeline.loading}
                  onClick={() => void loadTimeline(true)}
                >
                  Load older changes
                </Button>
                <p className="text-xs text-muted-foreground">
                  Missing entries do not prove nothing changed. Times use your browser's local
                  timezone.
                </p>
              </CardContent>
            </Card>
          </section>
        </TabsContent>

        <TabsContent {...panel("setup")}>
          <h2 id="lights-heading" className="text-2xl font-semibold tracking-tight">
            Setup
          </h2>
          <div className="grid gap-2">
            <p
              id="source-notice"
              className="rounded-lg border bg-muted/50 px-4 py-3 text-sm text-muted-foreground"
            >
              {!snapshot
                ? "Waiting for Home Assistant."
                : live
                  ? controlOn
                    ? "Home Assistant discovery. Home Keeper shows only the devices you select. Light control is on for monitored lights."
                    : "Home Assistant discovery. Home Keeper shows only the devices you select. Light control is off: Home Keeper only reads."
                  : controlOn
                    ? "Demo data only. No hardware or Home Assistant is connected. Light control is on for monitored lights."
                    : "Demo data only. No hardware or Home Assistant is connected. Light control is off: Home Keeper only reads."}
            </p>
            <p
              id="catalog-status"
              className="text-sm text-warning-foreground"
              role="status"
              aria-live="polite"
            >
              {snapshot && !snapshot.catalog
                ? "Devices cannot be listed while Home Assistant cannot be reached."
                : ""}
            </p>
          </div>
          <div className="grid gap-4 sm:grid-cols-3">
            <Card className="gap-1 px-5 py-4">
              <span className="text-sm text-muted-foreground">Home Assistant</span>
              <strong id="health" className="text-2xl font-semibold capitalize">
                {health ? health.status : "Connecting…"}
              </strong>
              <small id="heartbeat" className="text-xs text-muted-foreground">
                {health ? `Last checked: ${date(health.checked_at)}` : "Waiting for Home Assistant"}
              </small>
            </Card>
            <Card className="gap-1 px-5 py-4">
              <span className="text-sm text-muted-foreground">Supported devices</span>
              <strong id="count" className="text-2xl font-semibold tabular-nums">
                {snapshot?.catalog
                  ? String(snapshot.catalog.entities.filter((entity) => entity.supported).length)
                  : "—"}
              </strong>
              <small className="text-xs text-muted-foreground">
                Devices Home Keeper can monitor
              </small>
            </Card>
            <Card className="gap-1 px-5 py-4">
              <span className="text-sm text-muted-foreground">Monitoring</span>
              <strong id="selected-count" className="text-2xl font-semibold tabular-nums">
                {snapshot?.selection.entity_ids.length ?? 0}
              </strong>
              <small className="text-xs text-muted-foreground">
                You choose what Home Keeper shows
              </small>
            </Card>
          </div>
          <section aria-labelledby="choose-heading">
            <Card className="gap-4 py-5">
              <CardHeader className="px-5">
                <h3 id="choose-heading" className="leading-none font-semibold">
                  Choose devices to monitor
                </h3>
              </CardHeader>
              <CardContent className="grid gap-4 px-5">
                <Catalog
                  entities={snapshot?.catalog?.entities ?? null}
                  homeAssistantUrl={snapshot?.catalog?.home_assistant_url ?? null}
                  selected={snapshot?.selection.entity_ids ?? []}
                  onToggle={(id, selected) => void toggle(id, selected)}
                />
              </CardContent>
            </Card>
          </section>
        </TabsContent>
        {historyFor?.device_kind === "measurement" ? (
          <MeasurementDialog
            key={historyFor.id}
            entityId={historyFor.id}
            name={deviceName(historyFor)}
            onClose={() => setHistoryFor(null)}
          />
        ) : (
          historyFor && (
            <HistoryDialog
              key={historyFor.id}
              entityId={historyFor.id}
              name={deviceName(historyFor)}
              onClose={() => setHistoryFor(null)}
            />
          )
        )}
        <footer className="border-t pt-4 text-xs text-muted-foreground">
          History comes from Home Assistant · Changes show what happened, not why
        </footer>
      </main>
    </>
  );
  const agent = agentOpen && (
    <AgentSlot
      names={names}
      onClose={closeAgent}
      width={wide ? { expanded, toggle: toggleExpanded } : undefined}
    />
  );

  return (
    <Tabs
      value={tab}
      onValueChange={(value) => {
        const next = TABS.find((entry) => entry.id === value);
        if (next) openTab(next.id);
      }}
      // items-start: on a phone the rows keep their content's height under the sheet.
      className={wide ? "h-svh" : "grid min-h-svh grid-rows-[auto_1fr] items-start gap-0"}
    >
      {wide ? (
        // The cards reflow beside the panel; each side scrolls on its own.
        <ResizablePanelGroup
          orientation="horizontal"
          defaultLayout={savedLayout.defaultLayout}
          onLayoutChanged={savedLayout.onLayoutChanged}
        >
          <ResizablePanel id="dashboard" minSize={`${100 - AGENT_WIDTH.max}%`}>
            <div className="h-full overflow-y-auto">{dashboard}</div>
          </ResizablePanel>
          {agent && (
            <>
              <ResizableHandle withHandle aria-label="Resize agent panel" />
              <ResizablePanel
                id="agent"
                panelRef={agentPanel}
                defaultSize={`${AGENT_WIDTH.initial}%`}
                minSize={AGENT_MIN_PX}
                maxSize={`${AGENT_WIDTH.max}%`}
                onResize={(size) => setAgentShare(size.asPercentage)}
              >
                {agent}
              </ResizablePanel>
            </>
          )}
        </ResizablePanelGroup>
      ) : (
        <>
          {dashboard}
          {agent}
        </>
      )}
    </Tabs>
  );
}
