import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage, mutationHeaders, type Schemas, unwrap } from "./api/client";
import { Catalog, lightName } from "./components/Catalog";
import { Commands } from "./components/Commands";
import { Coverage, openGaps } from "./components/Coverage";
import { type LightCommand, Monitored } from "./components/Monitored";
import { Timeline } from "./components/Timeline";
import { date, time } from "./format";
import { useCommands, useTimeline } from "./useTimeline";

const POLL_MS = 3000;

const TABS = [
  { id: "home", label: "Home" },
  { id: "history", label: "History" },
  { id: "setup", label: "Setup" },
] as const;

type Tab = (typeof TABS)[number]["id"];

const tabFromHash = (): Tab =>
  TABS.find((tab) => `#${tab.id}` === window.location.hash)?.id ?? "home";

const collectionErrors: Record<NonNullable<Schemas["HealthReport"]["error"]>, string> = {
  authentication:
    "Home Assistant rejected authentication. Check the connector token and its user permissions.",
  connection:
    "The connector cannot reach Home Assistant. Check its address and container networking.",
  tls: "Certificate verification failed. Configure a trusted certificate; verification remains enabled.",
  timeout: "Home Assistant did not respond in time. Collection will retry.",
  invalid_data:
    "Home Assistant returned unsupported or invalid data. No affected observation was stored.",
  missing: "A selected entity could not be read. This does not prove a physical device outage.",
  discovery: "Discovery failed. The last validated catalog remains visible.",
  configuration: "The connector configuration is invalid. Check the origin and token file.",
};

// The API answers a command with a final outcome; `pending` and `interrupted`
// appear only in the command history, and read as "may not have changed" here.
const outcomeMessages: Record<
  Schemas["CommandRecord"]["outcome"],
  (name: string, command: LightCommand) => string
> = {
  observed: (name, command) =>
    command.action === "on" && command.brightness_pct != null
      ? `${name} is now on at ${command.brightness_pct}%.`
      : `${name} is now ${command.action}.`,
  not_observed: (name) => `Sent to ${name}, but no change was seen yet.`,
  refused: (name) => `Home Assistant refused the command for ${name}.`,
  unreachable: (name) => `Cannot reach the light controller. ${name} may not have changed.`,
  pending: (name) => `Sent to ${name}, but no change was seen yet.`,
  interrupted: (name) => `The command for ${name} was interrupted. ${name} may not have changed.`,
};

// How long a command's result stays on screen, unless another command replaces it.
const COMMAND_NOTICE_MS = 15000;

type CommandResult = { notice: string; reload: boolean };

// Matches `unwrap`, so a dropped connection reads like any other failed request.
const commandFailed: CommandResult = { notice: "Request failed. Please retry.", reload: false };

async function sendLightCommand(
  entity: Schemas["MonitoredEntity"],
  command: LightCommand,
): Promise<CommandResult> {
  const name = lightName(entity);
  const { data, error, response } = await api.POST("/api/lights/{entity_id}/command", {
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
  catalog: Schemas["CatalogResponse"];
  selection: Schemas["SelectionSnapshot"];
  monitored: Schemas["MonitoredEntities"];
  health: Schemas["HealthPending"] | Schemas["HealthReport"];
  coverage: Schemas["CoverageReport"];
};

// Polls overlap when a response is slow. Each load takes a number, and a result
// is applied only if no later load has applied its own.
type LoadOrder = { started: number; snapshot: number; control: number };

function catalogStatus({ freshness, refreshed_at }: Schemas["CatalogResponse"]) {
  return freshness === "stale"
    ? `Discovery is stale. Showing the last validated catalog from ${date(refreshed_at)}.`
    : `Discovery: ${freshness} · Last refreshed: ${date(refreshed_at)}`;
}

type DiscoveryState = {
  refreshPending: boolean;
  lastCatalogRefresh: string | null;
  requestedAfter: string | null;
};

export function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  // Apart from the snapshot, so a failed or off control read hides the toggles
  // even when another endpoint fails in the same poll.
  const [control, setControl] = useState<Schemas["ControlStatus"]["state"]>("off");
  const order = useRef<LoadOrder>({ started: 0, snapshot: 0, control: 0 });
  const [message, setMessage] = useState("");
  // Command results live apart from `message`, which every poll rewrites.
  const [commandNotice, setCommandNotice] = useState("");
  const noticeTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const timeline = useTimeline();
  const { load: loadTimeline, showingOlder } = timeline;
  const commands = useCommands();
  const { load: loadCommands, showingOlder: showingOlderCommands } = commands;
  // One mutation at a time, shared by selection toggles and discovery refresh.
  const busy = useRef(false);
  // A light command takes several seconds (the send, then up to 5 s of waiting for the
  // change), so commands lock per light, apart from `busy`.
  const sending = useRef(new Set<string>());
  const [sendingIds, setSendingIds] = useState<ReadonlySet<string>>(new Set());
  const discovery = useRef<DiscoveryState>({
    refreshPending: false,
    lastCatalogRefresh: null,
    requestedAfter: null,
  });

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
      const [catalog, selection, monitored, health, coverage] = await Promise.all([
        unwrap(api.GET("/api/catalog")),
        unwrap(api.GET("/api/selection")),
        unwrap(api.GET("/api/monitored")),
        unwrap(api.GET("/api/health")),
        unwrap(api.GET("/api/coverage", { params: { query: { limit: 20 } } })),
      ]);
      if (!isLatest("snapshot")) return;
      const state = discovery.current;
      state.lastCatalogRefresh = catalog.refreshed_at;
      if (
        state.refreshPending &&
        catalog.refreshed_at &&
        catalog.refreshed_at !== state.requestedAfter
      ) {
        state.refreshPending = false;
      }
      const healthError = health.status === "pending" ? null : health.error;
      setSnapshot({ catalog, selection, monitored, health, coverage });
      setMessage(
        (healthError && collectionErrors[healthError]) ||
          (state.refreshPending
            ? "Discovery refresh requested. The collector checks on its next collection pass."
            : ""),
      );
      await Promise.all([
        showingOlder.current ? null : loadTimeline(),
        showingOlderCommands.current ? null : loadCommands(),
      ]);
    } catch {
      if (!isLatest("snapshot")) return;
      setMessage(
        "Cannot reach Home Keeper. Collection freshness is unknown until connection returns.",
      );
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
    });

  const refreshDiscovery = () =>
    mutate(async () => {
      await unwrap(api.POST("/api/discovery/refresh", { headers: mutationHeaders }));
      discovery.current.requestedAfter = discovery.current.lastCatalogRefresh;
      discovery.current.refreshPending = true;
    });

  const showCommandNotice = (notice: string) => {
    clearTimeout(noticeTimer.current);
    setCommandNotice(notice);
    if (notice) noticeTimer.current = setTimeout(() => setCommandNotice(""), COMMAND_NOTICE_MS);
  };

  const sendCommand = async (entity: Schemas["MonitoredEntity"], command: LightCommand) => {
    if (sending.current.has(entity.id)) return;
    sending.current.add(entity.id);
    setSendingIds(new Set(sending.current));
    showCommandNotice("");
    let result: CommandResult;
    try {
      result = await sendLightCommand(entity, command);
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

  const live = snapshot?.catalog.source_id === "home_assistant";
  const controlOn = snapshot !== null && control === "on";
  const health = snapshot?.health;
  const names = new Map(
    [...(snapshot?.catalog.entities ?? []), ...(snapshot?.monitored.entities ?? [])].map(
      (entity) => [entity.id, lightName(entity)],
    ),
  );

  const interrupted = openGaps(snapshot?.coverage ?? null);
  const panel = (id: Tab) => ({
    id: `panel-${id}`,
    role: "tabpanel",
    "aria-labelledby": `tab-${id}`,
    hidden: tab !== id,
  });

  return (
    <>
      <header>
        <div className="brand">
          ◉ <strong>Home Keeper</strong>
        </div>
        <div className="tabs" role="tablist" aria-label="Views">
          {TABS.map(({ id, label }) => (
            <button
              key={id}
              id={`tab-${id}`}
              type="button"
              role="tab"
              aria-selected={tab === id}
              aria-controls={`panel-${id}`}
              onClick={() => openTab(id)}
            >
              {label}
            </button>
          ))}
        </div>
        <span id="source" className="demo">
          {snapshot ? (live ? "HOME ASSISTANT" : "SYNTHETIC DEMO") : "CONNECTING"}
        </span>
      </header>
      <main>
        <p id="message" className="message" role="status" aria-live="polite">
          {[commandNotice, message].filter(Boolean).join(" ")}
        </p>
        {interrupted.length > 0 && (
          <p id="interrupted" className="alert">
            {`Recording is interrupted since ${time(interrupted[0]?.last_confirmed_at)}. Lights may change without a record.`}{" "}
            <button type="button" className="quiet" onClick={() => openTab("history")}>
              See history
            </button>
          </p>
        )}

        <section {...panel("home")}>
          <p id="status-line" className="status-line">
            <span className={`dot ${health?.status ?? "pending"}`} aria-hidden="true" />
            {health
              ? `Collector ${health.status} · last reading ${time(health.status === "pending" ? null : health.received_at)}`
              : "Connecting…"}
            <span className="tag">{controlOn ? "CONTROL ON" : "READ ONLY"}</span>
          </p>
          <Monitored
            entities={snapshot?.monitored.entities ?? null}
            onStop={(id) => void toggle(id, false)}
            onSwitch={controlOn ? (entity, command) => void sendCommand(entity, command) : null}
            onChooseLights={() => openTab("setup")}
            sending={sendingIds}
          />
        </section>

        <section {...panel("history")}>
          <div className="section-heading">
            <h2 id="timeline-heading">History</h2>
            <button
              id="timeline-refresh"
              type="button"
              disabled={timeline.loading}
              onClick={() => void Promise.all([loadTimeline(), loadCommands()])}
            >
              Refresh history
            </button>
          </div>
          {(controlOn || Boolean(commands.commands?.length)) && (
            <section className="panel" aria-labelledby="commands-heading">
              <h3 id="commands-heading">Light commands</h3>
              <Commands
                commands={commands.commands}
                names={names}
                hasMore={commands.hasMore}
                loading={commands.loading}
                onMore={() => void loadCommands(true)}
              />
            </section>
          )}
          <section className="panel" aria-labelledby="changes-heading">
            <h3 id="changes-heading">Recorded changes</h3>
            <p id="timeline-status" className="history-status" role="status" aria-live="polite">
              {timeline.status}
            </p>
            <Timeline events={timeline.events} names={names} />
            <button
              id="timeline-more"
              type="button"
              hidden={!timeline.hasMore}
              disabled={timeline.loading}
              onClick={() => void loadTimeline(true)}
            >
              Load older changes
            </button>
            <p className="history-status">
              Missing entries do not prove nothing changed. Times use your browser's local timezone.
            </p>
          </section>
          <section className="panel" aria-labelledby="coverage-heading">
            <h3 id="coverage-heading">Collection gaps</h3>
            <Coverage coverage={snapshot?.coverage ?? null} />
          </section>
        </section>

        <section {...panel("setup")}>
          <div className="section-heading">
            <h2 id="lights-heading">Setup</h2>
            <button id="refresh" type="button" onClick={() => void refreshDiscovery()}>
              Refresh discovery
            </button>
          </div>
          <p id="source-notice" className="notice">
            {!snapshot
              ? "Waiting for the configured collector. No model or device controls are connected."
              : live
                ? controlOn
                  ? "Home Assistant discovery. Only selected individual Hue lights are recorded. Light control is on for monitored lights."
                  : "Home Assistant discovery. Only selected individual Hue lights are recorded. No model or device controls are connected."
                : controlOn
                  ? "Demo data only. No hardware, Home Assistant, or model is connected. Light control is on for monitored lights."
                  : "Demo data only. No hardware, Home Assistant, model, or device controls are connected."}
          </p>
          <p id="catalog-status" className="history-status" role="status" aria-live="polite">
            {snapshot && catalogStatus(snapshot.catalog)}
          </p>
          <div className="stats">
            <section>
              <span>Collector</span>
              <strong id="health">{health ? health.status : "Connecting…"}</strong>
              <small id="heartbeat">
                {health
                  ? `Last collection: ${date(health.status === "pending" ? null : health.received_at)}`
                  : "Waiting for collection"}
              </small>
            </section>
            <section>
              <span>Supported lamps</span>
              <strong id="count">
                {snapshot
                  ? String(snapshot.catalog.entities.filter((entity) => entity.supported).length)
                  : "—"}
              </strong>
              <small>Individual Hue lamps, without room groups</small>
            </section>
            <section>
              <span>Monitoring</span>
              <strong id="selected-count">{snapshot?.selection.entity_ids.length ?? 0}</strong>
              <small>You choose what gets recorded</small>
            </section>
          </div>
          <section className="panel" aria-labelledby="choose-heading">
            <h3 id="choose-heading">Choose lights to monitor</h3>
            <Catalog
              entities={snapshot?.catalog.entities ?? null}
              homeAssistantUrl={snapshot?.catalog.home_assistant_url ?? null}
              selected={snapshot?.selection.entity_ids ?? []}
              onToggle={(id, selected) => void toggle(id, selected)}
            />
          </section>
        </section>
        <footer>
          Stored on this machine · Change records are evidence, not explanations of cause
        </footer>
      </main>
    </>
  );
}
