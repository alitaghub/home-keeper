import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  type CommandRecord,
  errorMessage,
  mutationHeaders,
  type Schemas,
  unwrap,
} from "./api/client";
import { Catalog, lightName } from "./components/Catalog";
import { Commands } from "./components/Commands";
import { HistoryDialog } from "./components/History";
import { type DeviceCommand, Monitored } from "./components/Monitored";
import { Timeline } from "./components/Timeline";
import { date, time } from "./format";
import { useCommands, useTimeline } from "./useTimeline";
import { useRecentUsage } from "./useUsage";

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
  const usage = useRecentUsage(
    snapshot &&
      JSON.stringify(
        snapshot.monitored.entities.map(({ id, state }) => [
          id,
          state?.state,
          state?.device_kind === "light" ? state.brightness : null,
        ]),
      ),
  );

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
        {unreachable && (
          <p id="unreachable" className="alert" role="alert">
            <strong>Can't reach Home Assistant</strong>
            {health.error ? ` ${unreachableMessages[health.error]}` : ""}
          </p>
        )}

        <section {...panel("home")}>
          <p id="status-line" className="status-line">
            <span
              className={`dot ${health?.status === "unreachable" ? "failed" : (health?.status ?? "pending")}`}
              aria-hidden="true"
            />
            {health
              ? `Home Assistant ${health.status} · checked ${time(health.checked_at)}`
              : "Connecting…"}
            <span className="tag">{controlOn ? "CONTROL ON" : "READ ONLY"}</span>
          </p>
          <Monitored
            entities={monitoredEntities}
            usage={usage}
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
              <h3 id="commands-heading">Device commands</h3>
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
              {timeline.failed ? (
                "Cannot load recorded history. Previously displayed entries may be out of date."
              ) : (
                <>
                  History comes from Home Assistant. How far back it goes depends on its{" "}
                  <a href={recorderDocs} target="_blank" rel="noreferrer">
                    recorder settings
                  </a>
                  .
                </>
              )}
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
        </section>

        <section {...panel("setup")}>
          <div className="section-heading">
            <h2 id="lights-heading">Setup</h2>
          </div>
          <p id="source-notice" className="notice">
            {!snapshot
              ? "Waiting for Home Assistant."
              : live
                ? controlOn
                  ? "Home Assistant discovery. Home Keeper shows only the lights, plugs and switches you select. Light control is on for monitored lights."
                  : "Home Assistant discovery. Home Keeper shows only the lights, plugs and switches you select. Light control is off: Home Keeper only reads."
                : controlOn
                  ? "Demo data only. No hardware or Home Assistant is connected. Light control is on for monitored lights."
                  : "Demo data only. No hardware or Home Assistant is connected. Light control is off: Home Keeper only reads."}
          </p>
          <p id="catalog-status" className="history-status" role="status" aria-live="polite">
            {snapshot && !snapshot.catalog
              ? "Devices cannot be listed while Home Assistant cannot be reached."
              : ""}
          </p>
          <div className="stats">
            <section>
              <span>Home Assistant</span>
              <strong id="health">{health ? health.status : "Connecting…"}</strong>
              <small id="heartbeat">
                {health ? `Last checked: ${date(health.checked_at)}` : "Waiting for Home Assistant"}
              </small>
            </section>
            <section>
              <span>Supported devices</span>
              <strong id="count">
                {snapshot?.catalog
                  ? String(snapshot.catalog.entities.filter((entity) => entity.supported).length)
                  : "—"}
              </strong>
              <small>Single lights, plugs and switches, without groups or rooms</small>
            </section>
            <section>
              <span>Monitoring</span>
              <strong id="selected-count">{snapshot?.selection.entity_ids.length ?? 0}</strong>
              <small>You choose what Home Keeper shows</small>
            </section>
          </div>
          <section className="panel" aria-labelledby="choose-heading">
            <h3 id="choose-heading">Choose lights to monitor</h3>
            <Catalog
              entities={snapshot?.catalog?.entities ?? null}
              homeAssistantUrl={snapshot?.catalog?.home_assistant_url ?? null}
              selected={snapshot?.selection.entity_ids ?? []}
              onToggle={(id, selected) => void toggle(id, selected)}
            />
          </section>
        </section>
        {historyFor && (
          <HistoryDialog
            key={historyFor.id}
            entityId={historyFor.id}
            name={lightName(historyFor)}
            onClose={() => setHistoryFor(null)}
          />
        )}
        <footer>History comes from Home Assistant · Changes show what happened, not why</footer>
      </main>
    </>
  );
}
