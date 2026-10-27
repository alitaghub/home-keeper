import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage, mutationHeaders, type Schemas, unwrap } from "./api/client";
import { Catalog, lightName } from "./components/Catalog";
import { Coverage } from "./components/Coverage";
import { type LightAction, Monitored } from "./components/Monitored";
import { Timeline } from "./components/Timeline";
import { date } from "./format";
import { useTimeline } from "./useTimeline";

const POLL_MS = 3000;

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
  (name: string, action: LightAction) => string
> = {
  observed: (name, action) => `${name} is now ${action}.`,
  not_observed: (name) => `Sent to ${name}, but no change was seen yet.`,
  refused: (name) => `Home Assistant refused the command for ${name}.`,
  unreachable: (name) => `Cannot reach the light controller. ${name} may not have changed.`,
  pending: (name) => `Sent to ${name}, but no change was seen yet.`,
  interrupted: (name) => `The command for ${name} was interrupted. ${name} may not have changed.`,
};

// How long a command's result stays on screen, unless another command replaces it.
const COMMAND_NOTICE_MS = 15000;

// The API refuses a command it will not send with 409 and a fixed
// `{"detail": {"code", "message"}}`; schema.d.ts does not describe it.
function refusalMessage(body: unknown): string | null {
  if (typeof body !== "object" || body === null || !("detail" in body)) return null;
  const { detail } = body;
  return typeof detail === "object" &&
    detail !== null &&
    "message" in detail &&
    typeof detail.message === "string"
    ? detail.message
    : null;
}

type CommandResult = { notice: string; reload: boolean };

// Matches `unwrap`, so a dropped connection reads like any other failed request.
const commandFailed: CommandResult = { notice: "Request failed. Please retry.", reload: false };

async function sendLightCommand(
  entity: Schemas["MonitoredEntity"],
  action: LightAction,
): Promise<CommandResult> {
  const name = lightName(entity);
  const { data, error, response } = await api.POST("/api/lights/{entity_id}/command", {
    params: { path: { entity_id: entity.id } },
    body: { command: { action }, sender: { kind: "dashboard" } },
    headers: mutationHeaders,
  });
  if (response.ok && data) {
    return { notice: outcomeMessages[data.outcome](name, action), reload: true };
  }
  const refusal = response.status === 409 ? refusalMessage(error) : null;
  return refusal ? { notice: `${name}: ${refusal}`, reload: true } : commandFailed;
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
  // One mutation at a time, shared by selection toggles and discovery refresh.
  const busy = useRef(false);
  // Light commands take up to 5 seconds, so they lock per light, apart from `busy`.
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
      if (!showingOlder.current) await loadTimeline();
    } catch {
      if (!isLatest("snapshot")) return;
      setMessage(
        "Cannot reach Home Keeper. Collection freshness is unknown until connection returns.",
      );
    }
  }, [loadTimeline, showingOlder]);

  useEffect(() => () => clearTimeout(noticeTimer.current), []);

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

  const sendCommand = async (entity: Schemas["MonitoredEntity"], action: LightAction) => {
    if (sending.current.has(entity.id)) return;
    sending.current.add(entity.id);
    setSendingIds(new Set(sending.current));
    showCommandNotice("");
    let result: CommandResult;
    try {
      result = await sendLightCommand(entity, action);
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

  return (
    <>
      <header>
        <div className="brand">
          ◉ <strong>Home Keeper</strong>
        </div>
        <span id="source" className="demo">
          {snapshot ? (live ? "HOME ASSISTANT" : "SYNTHETIC DEMO") : "CONNECTING"}
        </span>
      </header>
      <main>
        <div className="heading">
          <div>
            <p className="eyebrow">YOUR HOME, OBSERVED</p>
            <h1>Know what is connected.</h1>
            <p className="intro">
              Discover devices. Choose the lights to monitor. Keep the evidence local.
            </p>
          </div>
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
        <p id="catalog-status" role="status" aria-live="polite">
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
        <p id="message" role="status" aria-live="polite">
          {[commandNotice, message].filter(Boolean).join(" ")}
        </p>
        <section className="panel" aria-labelledby="lights-heading">
          <div className="section-heading">
            <div>
              <h2 id="lights-heading">Your lamps</h2>
              <p>Choose which individual lamps to monitor.</p>
            </div>
          </div>
          <Catalog
            entities={snapshot?.catalog.entities ?? null}
            homeAssistantUrl={snapshot?.catalog.home_assistant_url ?? null}
            selected={snapshot?.selection.entity_ids ?? []}
            onToggle={(id, selected) => void toggle(id, selected)}
          />
        </section>
        <section className="panel" aria-labelledby="coverage-heading">
          <div className="section-heading">
            <div>
              <h2 id="coverage-heading">Collection gaps</h2>
              <p>Missing collection is separate from a light reporting unavailable.</p>
            </div>
          </div>
          <Coverage coverage={snapshot?.coverage ?? null} />
        </section>
        <section className="panel">
          <div className="section-heading">
            <div>
              <h2>Monitored lights</h2>
              <p>Latest observed state and collection freshness. Evidence type is shown below.</p>
            </div>
            <span className="tag">{controlOn ? "CONTROL ON" : "READ ONLY"}</span>
          </div>
          <Monitored
            entities={snapshot?.monitored.entities ?? null}
            onStop={(id) => void toggle(id, false)}
            onSwitch={controlOn ? (entity, action) => void sendCommand(entity, action) : null}
            sending={sendingIds}
          />
        </section>
        <section className="panel" aria-labelledby="timeline-heading">
          <div className="section-heading">
            <div>
              <h2 id="timeline-heading">Recorded changes</h2>
              <p>
                Selected lights' observed transitions, newest source event first. Earlier
                selections' history stays visible.
              </p>
            </div>
            <button
              id="timeline-refresh"
              type="button"
              disabled={timeline.loading}
              onClick={() => void loadTimeline()}
            >
              Refresh history
            </button>
          </div>
          <p id="timeline-status" className="history-status" role="status" aria-live="polite">
            {timeline.status}
          </p>
          <p className="history-status">
            Missing entries do not prove nothing changed. Review collection gaps above for missing
            intervals. Times use your browser's local timezone.
          </p>
          <Timeline events={timeline.events} />
          <button
            id="timeline-more"
            type="button"
            hidden={!timeline.hasMore}
            disabled={timeline.loading}
            onClick={() => void loadTimeline(true)}
          >
            Load older changes
          </button>
        </section>
        <footer>
          Stored on this machine · Change records are evidence, not explanations of cause · No agent
          in this milestone
        </footer>
      </main>
    </>
  );
}
