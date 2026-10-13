"use strict";
const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const date = (value) => value ? new Date(value).toLocaleString() : "Not captured";
let busy = false;
let refreshPending = false;
let lastCatalogRefresh = null;
let requestedAfter = null;
let timelineBusy = false;
let timelineCursor = null;
let showingOlder = false;
let timelineEvents = [];
const collectionErrors = {
  authentication: "Home Assistant rejected authentication. Check the connector token and its user permissions.",
  connection: "The connector cannot reach Home Assistant. Check its address and container networking.",
  tls: "Certificate verification failed. Configure a trusted certificate; verification remains enabled.",
  timeout: "Home Assistant did not respond in time. Collection will retry.",
  invalid_data: "Home Assistant returned unsupported or invalid data. No affected observation was stored.",
  missing: "A selected entity could not be read. This does not prove a physical device outage.",
  discovery: "Discovery failed. The last validated catalog remains visible.",
  configuration: "The connector configuration is invalid. Check the origin and token file."
};
async function request(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error("Request failed. Please retry.");
  return response.json();
}
async function toggle(id, selected) {
  if (busy) return;
  busy = true;
  try {
    await request(`/api/selection/${encodeURIComponent(id)}`, {
      method: "PUT", headers: {"Content-Type": "application/json", "X-Home-Observer": "1"},
      body: JSON.stringify({selected})
    });
    await load();
  } catch (error) { document.getElementById("message").textContent = error.message; }
  finally { busy = false; }
}
const lightName = (entity) => entity.device_name || entity.name;
function renderCatalog(entities, selected) {
  const lights = entities.filter((entity) => entity.supported);
  const others = entities.filter((entity) => !entity.supported);
  const target = document.getElementById("catalog");
  target.replaceChildren();
  if (!lights.length) target.append(el("p", entities.length
    ? "No supported lamps found. Check the Hue integration in Home Assistant."
    : "Waiting for discovery.", "empty"));
  for (const entity of lights) {
    const row = el("div", undefined, "entity-row lamp-card");
    const name = lightName(entity);
    const checked = selected.includes(entity.id);
    row.append(el("strong", name));
    const button = el("button", checked ? "Stop monitoring" : "Monitor", checked ? "secondary" : "primary");
    button.setAttribute("aria-label", `${checked ? "Stop monitoring" : "Monitor"} ${name}`);
    button.addEventListener("click", () => toggle(entity.id, !checked));
    row.append(button);
    target.append(row);
  }
  document.getElementById("other-summary").textContent = `Show other entities (${others.length})`;
  document.getElementById("other-entities").hidden = !others.length;
  const otherTarget = document.getElementById("other-catalog");
  otherTarget.replaceChildren();
  const groups = new Map();
  for (const entity of others) {
    const key = entity.device_id || "__ungrouped";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entity);
  }
  for (const [key, group] of groups) {
    const card = el("div", undefined, "device");
    card.append(el("h3", key === "__ungrouped" ? "Ungrouped entities" : group[0].device_name || key));
    for (const entity of group) {
      const row = el("div", undefined, "entity-row");
      const text = el("div");
      text.append(el("strong", entity.name), el("small", entity.id));
      text.append(el("small", entity.reason, "unsupported"));
      row.append(text, el("span", "Unsupported", "tag"));
      card.append(row);
    }
    otherTarget.append(card);
  }
}
function renderMonitored(entities) {
  const target = document.getElementById("monitored");
  target.replaceChildren();
  if (!entities.length) target.append(el("p", "No lights selected. Choose a light below to establish its first baseline.", "empty"));
  for (const entity of entities) {
    const card = el("article", undefined, "observation");
    const title = el("div", undefined, "section-heading");
    title.append(el("h3", lightName(entity)), el("span", entity.freshness, `tag ${entity.freshness}`));
    const stop = el("button", "Stop monitoring", "secondary");
    stop.setAttribute("aria-label", `Stop monitoring ${lightName(entity)}`);
    stop.addEventListener("click", () => toggle(entity.id, false));
    title.append(stop);
    card.append(title);
    const baseline = entity.baseline;
    card.append(el("p", baseline ? `${baseline.state} · ${baseline.availability}` : "Pending · state unknown", "state"));
    if (baseline) {
      const kind = {baseline: "Initial baseline", reconciliation: "Polling reconciliation snapshot", event: "Latest recorded event", recovery: "Recovery snapshot; transition time unknown"}[baseline.kind] || "State snapshot";
      card.append(el("small", kind));
      if (baseline.brightness != null) card.append(el("p", `Brightness ${baseline.brightness} / 255`));
      if (baseline.coverage_gap_ids?.length) card.append(el("small", `Related gap evidence: ${baseline.coverage_gap_ids.join(", ")}. Changes during this gap have unknown timing.`));
      card.append(el("small", `Source: ${baseline.source_id} · Evidence ID: ${baseline.id}`));
      card.append(el("small", `Latest confirmed collection: ${date(entity.confirmed_at)}`));
      card.append(el("small", `Evidence received: ${date(baseline.received_at)} · Stored: ${date(baseline.persisted_at)}`));
      card.append(el("small", `Source last changed: ${date(baseline.last_changed)}`));
    } else card.append(el("p", "The collector has not confirmed a selected observation yet."));
    target.append(card);
  }
}
const changeFields = ["state", "availability", "brightness", "color_mode", "color_temp_kelvin", "hs_color", "xy_color", "rgb_color", "rgbw_color", "rgbww_color"];
const valueText = (value) => value == null ? "Not captured" : Array.isArray(value) ? value.join(", ") : String(value);
function renderTimeline(events) {
  const target = document.getElementById("timeline");
  target.replaceChildren();
  if (!events.length) target.append(el("p", "No recorded changes in retained history. Select a light and wait for a change. Earlier changes may be missing.", "empty"));
  for (const event of events) {
    const card = el("article", undefined, "observation recorded-event");
    card.append(el("h3", event.source_entity_id || event.entity_id));
    const changes = el("ul", undefined, "change-values");
    for (const field of changeFields) {
      const before = event.old?.[field];
      const after = event.new?.[field];
      if (JSON.stringify(before ?? null) !== JSON.stringify(after ?? null)) {
        changes.append(el("li", `${field.replaceAll("_", " ")}: ${valueText(before)} → ${valueText(after)}`));
      }
    }
    if (!changes.childElementCount) changes.append(el("li", "No changed field captured."));
    card.append(changes);
    card.append(el("small", `Source: ${event.source_id} · Entity identity: ${event.entity_id}`));
    card.append(el("small", `Evidence ID: ${event.id}`, "evidence-id"));
    card.append(el("small", `Source event: ${date(event.event_time)}`));
    card.append(el("small", `Connector received: ${date(event.received_at)} · Stored: ${date(event.persisted_at)}`));
    for (const field of ["last_changed", "last_updated", "last_reported"]) {
      if (event.new?.[field]) card.append(el("small", `New state source ${field.replaceAll("_", " ")}: ${date(event.new[field])}`));
    }
    target.append(card);
  }
}
async function loadTimeline(older = false) {
  if (timelineBusy) return;
  timelineBusy = true;
  const more = document.getElementById("timeline-more");
  const refresh = document.getElementById("timeline-refresh");
  more.disabled = true;
  refresh.disabled = true;
  try {
    const params = new URLSearchParams({limit: "50"});
    if (older && timelineCursor) params.set("cursor", timelineCursor);
    const history = await request(`/api/events?${params}`);
    if (!older) {
      timelineEvents = [];
      showingOlder = false;
    } else showingOlder = true;
    const ids = new Set(timelineEvents.map((event) => event.id));
    timelineEvents.push(...history.events.filter((event) => !ids.has(event.id)));
    timelineCursor = history.next_cursor;
    more.hidden = !timelineCursor;
    renderTimeline(timelineEvents);
    document.getElementById("timeline-status").textContent = `${timelineEvents.length} changes shown. Retention cutoff: ${date(history.retained_since)}. This cutoff does not establish continuous coverage.${showingOlder ? " Older history is open; refresh to see new changes." : " New changes refresh automatically."}`;
  } catch (_) {
    document.getElementById("timeline-status").textContent = "Cannot load recorded history. Previously displayed entries may be out of date.";
  } finally {
    timelineBusy = false;
    more.disabled = false;
    refresh.disabled = false;
  }
}
const gapReasons = {
  connection: "Connector connection lost", ingestion: "Observations could not be saved",
  authentication: "Authentication failed", timeout: "Collection timed out",
  restart: "Application restarted", overflow: "Recovery buffer exceeded its limit",
  reconciliation: "Snapshot differed from recorded state", heartbeat: "Collector heartbeat missing",
  selection: "Monitoring selection changed", invalid_data: "Source data was invalid",
  configuration: "Connector configuration needs attention", tls: "Certificate verification failed",
  missing: "Selected entity could not be read"
};
function renderCoverage(coverage) {
  document.getElementById("coverage-status").textContent =
    `Current collection: ${coverage.state}. Last confirmed: ${date(coverage.last_confirmed_at)}. History outside recorded collection windows is unknown.`;
  const target = document.getElementById("coverage");
  target.replaceChildren();
  if (!coverage.gaps.length) target.append(el("p", "No retained interruption records. This does not prove continuous history.", "empty"));
  for (const gap of coverage.gaps) {
    const card = el("article", undefined, "observation");
    card.id = `gap-${gap.id}`;
    card.append(el("h3", gap.recovered_at ? "Collection resumed" : "Collection interrupted"));
    card.append(el("p", gapReasons[gap.reason] || "Collection continuity is uncertain"));
    card.append(el("small", `Possible missing history since: ${date(gap.last_confirmed_at)}. The exact start is unknown.`));
    card.append(el("small", `Interruption noticed: ${date(gap.detected_at)}`));
    card.append(el("small", gap.recovered_at ? `Recovery completed: ${date(gap.recovered_at)}` : "Recovery pending. Current evidence may be stale."));
    card.append(el("small", `Source: ${gap.source_id} · Entities: ${gap.entity_ids.join(", ") || "None selected"}`));
    card.append(el("small", `Gap evidence ID: ${gap.id}`));
    target.append(card);
  }
}
async function load() {
  try {
    const [catalog, selection, monitored, health, coverage] = await Promise.all([
      request("/api/catalog"), request("/api/selection"), request("/api/monitored"), request("/api/health"), request("/api/coverage?limit=20")
    ]);
    document.getElementById("count").textContent = String(catalog.entities.filter((entity) => entity.supported).length);
    document.getElementById("selected-count").textContent = String(selection.entity_ids.length);
    document.getElementById("health").textContent = health.status;
    document.getElementById("heartbeat").textContent = `Last collection: ${date(health.received_at)}`;
    const live = catalog.source_id === "home_assistant";
    lastCatalogRefresh = catalog.refreshed_at;
    if (refreshPending && catalog.refreshed_at && catalog.refreshed_at !== requestedAfter) refreshPending = false;
    document.getElementById("source").textContent = live ? "HOME ASSISTANT" : "SYNTHETIC DEMO";
    document.getElementById("source-notice").textContent = live
      ? "Home Assistant discovery. Only selected individual Hue lights are recorded. No model or device controls are connected."
      : "Demo data only. No hardware, Home Assistant, model, or device controls are connected.";
    document.getElementById("catalog-status").textContent = catalog.freshness === "stale"
      ? `Discovery is stale. Showing the last validated catalog from ${date(catalog.refreshed_at)}.`
      : `Discovery: ${catalog.freshness || "pending"} · Last refreshed: ${date(catalog.refreshed_at)}`;
    document.getElementById("message").textContent = collectionErrors[health.error]
      || (refreshPending ? "Discovery refresh requested. The collector checks on its next collection pass." : "");
    renderCatalog(catalog.entities, selection.entity_ids);
    renderMonitored(monitored.entities);
    renderCoverage(coverage);
    if (!showingOlder) await loadTimeline();
  } catch (_) { document.getElementById("message").textContent = "Cannot reach the observer. Collection freshness is unknown until connection returns."; }
}
document.getElementById("refresh").addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  try {
    await request("/api/discovery/refresh", {method: "POST", headers: {"X-Home-Observer": "1"}});
    requestedAfter = lastCatalogRefresh;
    refreshPending = true;
    await load();
  } catch (error) { document.getElementById("message").textContent = error.message; }
  finally { busy = false; }
});
document.getElementById("timeline-refresh").addEventListener("click", () => loadTimeline());
document.getElementById("timeline-more").addEventListener("click", () => loadTimeline(true));
load();
setInterval(load, 3000);
