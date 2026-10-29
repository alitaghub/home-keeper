// API responses for dashboard tests. Shapes and values follow the recorded API
// snapshots in apps/keeper/tests/integration/snapshots/api/*.json.
import type { components } from "../api/schema";

type Schemas = components["schemas"];
// A light's or a switch's command record. The union has no schema name of its own.
type CommandRecord = Schemas["CommandsPage"]["commands"][number];

export const desk = {
  id: "light.hue_desk",
  device_id: "hue-desk",
  device_name: "Desk lamp",
  name: "Desk light",
  supported: true,
  reason: null,
  device_kind: "light",
} satisfies Schemas["Entity"];

export const reading = {
  id: "light.hue_reading",
  device_id: "hue-reading",
  device_name: "Reading lamp",
  name: "Reading light",
  supported: true,
  reason: null,
  device_kind: "light",
} satisfies Schemas["Entity"];

export const roomGroup = {
  id: "light.hue_room",
  device_id: "hue-room",
  device_name: "Living room group",
  name: "All living room lights",
  supported: false,
  reason: "Groups and rooms are not recorded",
  device_kind: "light",
} satisfies Schemas["Entity"];

export const ungroupedSensor = {
  id: "sensor.ungrouped",
  device_id: null,
  device_name: null,
  name: "Ungrouped sensor",
  supported: false,
  reason: "Only lights, plugs and switches are supported",
} satisfies Schemas["Entity"];

export const smartPlug = {
  id: "switch.smart_plug",
  device_id: "smart-plug",
  device_name: "Smart plug",
  name: "Plug",
  supported: true,
  reason: null,
  device_kind: "switch",
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
  device_kind: "light",
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
  old: { device_kind: "light", availability: "available", state: "off" },
  new: { device_kind: "light", availability: "available", state: "on", brightness: 150 },
} satisfies Schemas["EventRecord"];

export const change2 = {
  ...changeEvent,
  id: "change-2",
  event_time: "2026-10-12T09:59:40Z",
  received_at: "2026-10-12T09:59:40Z",
  old: { device_kind: "light", availability: "available", state: "on" },
  new: { device_kind: "light", availability: "available", state: "off" },
} satisfies Schemas["EventRecord"];

export const change1 = {
  ...changeEvent,
  id: "change-1",
  event_time: "2026-10-12T09:59:30Z",
  received_at: "2026-10-12T09:59:30Z",
  old: { device_kind: "light", availability: "available", state: "off" },
  new: { device_kind: "light", availability: "available", state: "on", brightness: 50 },
} satisfies Schemas["EventRecord"];

// Distinct source, receipt, and storage times so a swapped label shows up.
export const colorChange = {
  ...changeEvent,
  id: "change-color",
  event_time: "2026-10-12T09:59:55Z",
  received_at: "2026-10-12T09:59:57Z",
  persisted_at: "2026-10-12T10:00:01+00:00",
  old: {
    device_kind: "light",
    availability: "available",
    state: "on",
    brightness: 150,
    xy_color: [0.3, 0.32],
  },
  new: {
    device_kind: "light",
    availability: "available",
    state: "on",
    brightness: 150,
    xy_color: [0.45, 0.41],
  },
} satisfies Schemas["EventRecord"];

// The smart plug turning on: a switch reading has a state but no light attributes.
export const plugChange = {
  ...changeEvent,
  device_kind: "switch",
  entity_id: smartPlug.id,
  id: "change-plug",
  event_time: "2026-10-12T09:59:58Z",
  received_at: "2026-10-12T09:59:58Z",
  old: { device_kind: "switch", availability: "available", state: "off" },
  new: { device_kind: "switch", availability: "available", state: "on" },
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

export const controlOff = { state: "off", heartbeat_at: null } satisfies Schemas["ControlStatus"];

export const controlOn = {
  state: "on",
  heartbeat_at: "2026-10-12T10:00:08+00:00",
} satisfies Schemas["ControlStatus"];

/** A monitored light whose recorded reading has the given state and availability. */
export const monitoredLight = (
  entity: Schemas["Entity"],
  state: Schemas["LightBaseline"]["state"],
  availability: Schemas["LightBaseline"]["availability"] = "available",
) =>
  ({
    ...entity,
    baseline: {
      kind: "baseline",
      device_kind: "light",
      id: `baseline-${entity.id}`,
      entity_id: entity.id,
      source_entity_id: null,
      source_id: "demo",
      state,
      availability,
      collection_started_at: null,
      coverage_gap_ids: [],
      received_at: "2026-10-12T10:00:06Z",
      persisted_at: "2026-10-12T10:00:06+00:00",
    },
    freshness: "fresh",
    confirmed_at: "2026-10-12T10:00:06+00:00",
  }) satisfies Schemas["MonitoredEntity"];

/** A monitored plug or switch whose recorded reading has the given state and availability. */
export const monitoredSwitch = (
  entity: Schemas["Entity"] & { device_kind: "switch" },
  state: Schemas["SwitchBaseline"]["state"],
  availability: Schemas["SwitchBaseline"]["availability"] = "available",
) =>
  ({
    ...entity,
    baseline: {
      kind: "baseline",
      device_kind: "switch",
      id: `baseline-${entity.id}`,
      entity_id: entity.id,
      source_entity_id: null,
      source_id: "demo",
      state,
      availability,
      collection_started_at: null,
      coverage_gap_ids: [],
      received_at: "2026-10-12T10:00:06Z",
      persisted_at: "2026-10-12T10:00:06+00:00",
    },
    freshness: "fresh",
    confirmed_at: "2026-10-12T10:00:06+00:00",
  }) satisfies Schemas["MonitoredEntity"];

/** The command record the controller returns for a light command. */
export const commandRecord = (
  outcome: CommandRecord["outcome"],
  action: Schemas["LightCommandRecord"]["command"]["action"] = "on",
) =>
  ({
    id: "command-1",
    entity_id: desk.id,
    device_kind: "light",
    command: { action },
    sender: { kind: "dashboard" },
    sent_at: "2026-10-12T10:01:00+00:00",
    outcome,
    event_id: outcome === "observed" ? "change-4" : null,
  }) satisfies Schemas["LightCommandRecord"];

/** The command record the controller returns for a smart plug command. */
export const switchCommandRecord = (
  outcome: CommandRecord["outcome"],
  action: Schemas["SwitchCommandRecord"]["command"]["action"] = "on",
) =>
  ({
    id: "command-plug-1",
    entity_id: smartPlug.id,
    device_kind: "switch",
    command: { action },
    sender: { kind: "dashboard" },
    sent_at: "2026-10-12T10:01:00+00:00",
    outcome,
    event_id: outcome === "observed" ? "change-plug" : null,
  }) satisfies Schemas["SwitchCommandRecord"];

/** A command history row; override any field. */
export const historyCommand = (
  id: string,
  overrides: Partial<Schemas["LightCommandRecord"]> = {},
): Schemas["LightCommandRecord"] => ({
  id,
  entity_id: desk.id,
  device_kind: "light",
  command: { action: "on" },
  sender: { kind: "dashboard" },
  sent_at: "2026-10-12T10:01:00+00:00",
  outcome: "not_observed",
  event_id: null,
  ...overrides,
});

export const commandsPage = (commands: CommandRecord[], next_cursor: string | null = null) =>
  ({
    commands,
    next_cursor,
    retained_since: "2026-09-12T10:00:00+00:00",
  }) satisfies Schemas["CommandsPage"];

export const emptyCommands = commandsPage([]);

const hourly = (hour: number, seconds: number) =>
  Array.from({ length: 24 }, (_, index) => (index === hour ? seconds : 0));

/** The last day's usage: the desk lamp on 1 h at 75%, with a 30-minute gap; the plug on 2 h. */
export const usage = {
  start: "2026-10-11T10:00:00+00:00",
  end: "2026-10-12T10:00:00+00:00",
  timezone: "UTC",
  retained_since: "2026-09-12T10:00:00+00:00",
  devices: [
    {
      device_kind: "light",
      entity_id: desk.id,
      on_seconds: 3600,
      unknown_seconds: 1800,
      switch_ons: 2,
      average_brightness: 191.25,
      days: [
        { date: "2026-10-11", on_seconds: 1800, unknown_seconds: 1800 },
        { date: "2026-10-12", on_seconds: 1800, unknown_seconds: 0 },
      ],
      hours: hourly(10, 3600),
      unknown_intervals: [
        { start: "2026-10-11T12:00:00+00:00", end: "2026-10-11T12:30:00+00:00", reason: "gap" },
      ],
      periods: [
        {
          start: "2026-10-11T10:00:00+00:00",
          end: "2026-10-11T11:00:00+00:00",
          state: "on",
          brightness: 191,
        },
        {
          start: "2026-10-11T11:00:00+00:00",
          end: "2026-10-11T12:00:00+00:00",
          state: "off",
          brightness: null,
        },
        {
          start: "2026-10-11T12:30:00+00:00",
          end: "2026-10-12T10:00:00+00:00",
          state: "off",
          brightness: null,
        },
      ],
    },
    {
      device_kind: "switch",
      entity_id: smartPlug.id,
      on_seconds: 7200,
      unknown_seconds: 0,
      switch_ons: 1,
      days: [
        { date: "2026-10-11", on_seconds: 7200, unknown_seconds: 0 },
        { date: "2026-10-12", on_seconds: 0, unknown_seconds: 0 },
      ],
      hours: hourly(20, 7200),
      unknown_intervals: [],
      periods: [
        { start: "2026-10-11T10:00:00+00:00", end: "2026-10-11T20:00:00+00:00", state: "off" },
        { start: "2026-10-11T20:00:00+00:00", end: "2026-10-11T22:00:00+00:00", state: "on" },
        { start: "2026-10-11T22:00:00+00:00", end: "2026-10-12T10:00:00+00:00", state: "off" },
      ],
    },
  ],
} satisfies Schemas["UsageReport"];

export const emptyUsage = { ...usage, devices: [] } satisfies Schemas["UsageReport"];
