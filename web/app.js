"use strict";
const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const date = (value) => value ? new Date(value).toLocaleString() : "Not captured";
let busy = false;
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
function renderCatalog(entities, selected) {
  const target = document.getElementById("catalog");
  target.replaceChildren();
  if (!entities.length) target.append(el("p", "Waiting for the synthetic collector to publish discovery.", "empty"));
  const groups = new Map();
  for (const entity of entities) {
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
      if (!entity.supported) text.append(el("small", entity.reason, "unsupported"));
      row.append(text);
      if (entity.supported) {
        const checked = selected.includes(entity.id);
        const button = el("button", checked ? "Stop monitoring" : "Monitor", checked ? "secondary" : "primary");
        button.setAttribute("aria-label", `${checked ? "Stop monitoring" : "Monitor"} ${entity.name}`);
        button.addEventListener("click", () => toggle(entity.id, !checked));
        row.append(button);
      } else row.append(el("span", "Unsupported", "tag"));
      card.append(row);
    }
    target.append(card);
  }
}
function renderMonitored(entities) {
  const target = document.getElementById("monitored");
  target.replaceChildren();
  if (!entities.length) target.append(el("p", "No lights selected. Choose a light below to establish its first baseline.", "empty"));
  for (const entity of entities) {
    const card = el("article", undefined, "observation");
    const title = el("div", undefined, "section-heading");
    title.append(el("h3", entity.name), el("span", entity.freshness, `tag ${entity.freshness}`));
    card.append(title);
    const baseline = entity.baseline;
    card.append(el("p", baseline ? `${baseline.state} · ${baseline.availability}` : "Pending · state unknown", "state"));
    if (baseline) {
      if (baseline.brightness != null) card.append(el("p", `Brightness ${baseline.brightness} / 255`));
      card.append(el("small", `Source: ${baseline.source_id} · Evidence ID: ${baseline.id}`));
      card.append(el("small", `Latest confirmed collection: ${date(entity.confirmed_at)}`));
      card.append(el("small", `Baseline received: ${date(baseline.received_at)} · Stored: ${date(baseline.persisted_at)}`));
      card.append(el("small", `Source last changed: ${date(baseline.last_changed)}`));
    } else card.append(el("p", "The collector has not confirmed a selected observation yet."));
    target.append(card);
  }
}
async function load() {
  try {
    const [catalog, selection, monitored, health] = await Promise.all([
      request("/api/catalog"), request("/api/selection"), request("/api/monitored"), request("/api/health")
    ]);
    document.getElementById("count").textContent = String(catalog.entities.length);
    document.getElementById("selected-count").textContent = String(selection.entity_ids.length);
    document.getElementById("health").textContent = health.status;
    document.getElementById("heartbeat").textContent = `Last collection: ${date(health.received_at)}`;
    document.getElementById("message").textContent = "";
    renderCatalog(catalog.entities, selection.entity_ids);
    renderMonitored(monitored.entities);
  } catch (_) { document.getElementById("message").textContent = "Cannot reach the observer. Collection freshness is unknown until connection returns."; }
}
document.getElementById("refresh").addEventListener("click", load);
load();
setInterval(load, 3000);
