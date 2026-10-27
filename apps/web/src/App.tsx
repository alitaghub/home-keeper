import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage, mutationHeaders, type Schemas, unwrap } from "./api/client";
import { Catalog } from "./components/Catalog";
import { Coverage } from "./components/Coverage";
import { Monitored } from "./components/Monitored";
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

type Snapshot = {
  catalog: Schemas["CatalogResponse"];
  selection: Schemas["SelectionSnapshot"];
  monitored: Schemas["MonitoredEntities"];
  health: Schemas["HealthPending"] | Schemas["HealthReport"];
  coverage: Schemas["CoverageReport"];
};

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
  const [message, setMessage] = useState("");
  const timeline = useTimeline();
  const { load: loadTimeline, showingOlder } = timeline;
  // One mutation at a time, shared by selection toggles and discovery refresh.
  const busy = useRef(false);
  const discovery = useRef<DiscoveryState>({
    refreshPending: false,
    lastCatalogRefresh: null,
    requestedAfter: null,
  });

  const load = useCallback(async () => {
    try {
      const [catalog, selection, monitored, health, coverage] = await Promise.all([
        unwrap(api.GET("/api/catalog")),
        unwrap(api.GET("/api/selection")),
        unwrap(api.GET("/api/monitored")),
        unwrap(api.GET("/api/health")),
        unwrap(api.GET("/api/coverage", { params: { query: { limit: 20 } } })),
      ]);
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
      setMessage(
        "Cannot reach Home Keeper. Collection freshness is unknown until connection returns.",
      );
    }
  }, [loadTimeline, showingOlder]);

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

  const live = snapshot?.catalog.source_id === "home_assistant";
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
              ? "Home Assistant discovery. Only selected individual Hue lights are recorded. No model or device controls are connected."
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
          {message}
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
            <span className="tag">READ ONLY</span>
          </div>
          <Monitored
            entities={snapshot?.monitored.entities ?? null}
            onStop={(id) => void toggle(id, false)}
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
