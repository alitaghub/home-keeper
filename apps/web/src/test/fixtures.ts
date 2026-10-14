// API responses for dashboard tests. Shapes and values follow the recorded API
// snapshots in apps/observer/tests/integration/snapshots/api/*.json.
import type { components } from "../api/schema";

type Schemas = components["schemas"];

export const desk = {
  id: "light.hue_desk",
  device_id: "hue-desk",
  device_name: "Desk lamp",
  name: "Desk light",
  supported: true,
  reason: null,
} satisfies Schemas["Entity"];

export const reading = {
  id: "light.hue_reading",
  device_id: "hue-reading",
  device_name: "Reading lamp",
  name: "Reading light",
  supported: true,
  reason: null,
} satisfies Schemas["Entity"];

export const roomGroup = {
  id: "light.hue_room",
  device_id: "hue-room",
  device_name: "Living room group",
  name: "All living room lights",
  supported: false,
  reason: "Room aggregate, not an individual bulb",
} satisfies Schemas["Entity"];

export const ungroupedSensor = {
  id: "sensor.ungrouped",
  device_id: null,
  device_name: null,
  name: "Ungrouped sensor",
  supported: false,
  reason: "Sensor monitoring is outside this milestone",
} satisfies Schemas["Entity"];

export const catalog = {
  source_id: "demo",
  home_assistant_url: null,
  freshness: "fresh",
  refreshed_at: "2026-10-12T10:00:07+00:00",
  entities: [desk, reading, roomGroup, ungroupedSensor],
} satisfies Schemas["CatalogResponse"];

export const emptySelection = {
  entity_ids: [],
  refresh_revision: 0,
  revision: 0,
} satisfies Schemas["SelectionSnapshot"];

export const emptyMonitored = {
  entities: [],
} satisfies Schemas["MonitoredEntities"];

export const health = {
  status: "healthy",
  error: null,
  source_id: "demo",
  received_at: "2026-10-12T10:00:08Z",
  persisted_at: "2026-10-12T10:00:08+00:00",
} satisfies Schemas["HealthReport"];

export const pendingHealth = {
  status: "pending",
  source_id: "demo",
} satisfies Schemas["HealthPending"];

export const openGap = {
  id: "gap-open",
  reason: "reconciliation",
  source_id: "demo",
  entity_ids: ["light.hue_desk"],
  last_confirmed_at: "2026-10-12T10:00:06+00:00",
  detected_at: "2026-10-12T10:00:08+00:00",
  recovered_at: null,
  uncertain_start: true,
} satisfies Schemas["CoverageGap"];

export const recoveredGap = {
  id: "gap-recovered",
  reason: "connection",
  source_id: "demo",
  entity_ids: ["light.hue_desk", "light.hue_reading"],
  last_confirmed_at: "2026-10-12T10:00:00+00:00",
  detected_at: "2026-10-12T10:00:05+00:00",
  recovered_at: "2026-10-12T10:00:06+00:00",
  uncertain_start: true,
} satisfies Schemas["CoverageGap"];

export const coverage = {
  continuous_coverage: false,
  outside_recorded_windows: "unknown",
  state: "interrupted",
  last_confirmed_at: "2026-10-12T10:00:08+00:00",
  retained_since: "2026-09-12T10:00:10+00:00",
  gaps: [openGap, recoveredGap],
} satisfies Schemas["CoverageReport"];

export const emptyCoverage = {
  continuous_coverage: false,
  outside_recorded_windows: "unknown",
  state: "unknown",
  last_confirmed_at: null,
  retained_since: "2026-09-12T10:00:00+00:00",
  gaps: [],
} satisfies Schemas["CoverageReport"];

const changeEvent = {
  entity_id: "light.hue_desk",
  source_entity_id: null,
  source_id: "demo",
  selection_revision: 2,
  persisted_at: "2026-10-12T10:00:00+00:00",
} satisfies Partial<Schemas["EventRecord"]>;

export const change3 = {
  ...changeEvent,
  id: "change-3",
  event_time: "2026-10-12T09:59:50Z",
  received_at: "2026-10-12T09:59:50Z",
  old: { availability: "available", state: "off" },
  new: { availability: "available", state: "on", brightness: 150 },
} satisfies Schemas["EventRecord"];

export const change2 = {
  ...changeEvent,
  id: "change-2",
  event_time: "2026-10-12T09:59:40Z",
  received_at: "2026-10-12T09:59:40Z",
  old: { availability: "available", state: "on" },
  new: { availability: "available", state: "off" },
} satisfies Schemas["EventRecord"];

export const change1 = {
  ...changeEvent,
  id: "change-1",
  event_time: "2026-10-12T09:59:30Z",
  received_at: "2026-10-12T09:59:30Z",
  old: { availability: "available", state: "off" },
  new: { availability: "available", state: "on", brightness: 50 },
} satisfies Schemas["EventRecord"];

// Distinct source, receipt, and storage times so a swapped label shows up.
export const colorChange = {
  ...changeEvent,
  id: "change-color",
  event_time: "2026-10-12T09:59:55Z",
  received_at: "2026-10-12T09:59:57Z",
  persisted_at: "2026-10-12T10:00:01+00:00",
  old: { availability: "available", state: "on", brightness: 150, xy_color: [0.3, 0.32] },
  new: { availability: "available", state: "on", brightness: 150, xy_color: [0.45, 0.41] },
} satisfies Schemas["EventRecord"];

export const eventsPage1 = {
  coverage: "unknown",
  events: [change3, change2],
  next_cursor: "2026-10-12T09:59:40Z|change-2",
  retained_since: "2026-09-12T10:00:10+00:00",
} satisfies Schemas["EventsPage"];

export const eventsPage2 = {
  coverage: "unknown",
  events: [change1],
  next_cursor: null,
  retained_since: "2026-09-12T10:00:10+00:00",
} satisfies Schemas["EventsPage"];

export const emptyEvents = {
  coverage: "unknown",
  events: [],
  next_cursor: null,
  retained_since: "2026-09-12T10:00:00+00:00",
} satisfies Schemas["EventsPage"];

export const discoveryRefresh = {
  refresh_revision: 1,
} satisfies Schemas["DiscoveryRefresh"];

/** The dashboard's `toLocaleString` rendering of an API timestamp. */
export const localTime = (value: string): string => new Date(value).toLocaleString();
