// API responses for dashboard tests. Shapes and values follow the recorded API
// snapshots in apps/keeper/tests/integration/snapshots/api/*.json.
import type { components } from "../api/schema";
import type { ResponseBody } from "./fake-api";

type Schemas = components["schemas"];
// A light's or a switch's command record. The union has no schema name of its own.
type CommandRecord = Schemas["CommandsPage"]["commands"][number];
// Taken from the routes, so a renamed schema does not break the fixtures.
type Health = ResponseBody<"GET /api/health">;
type MonitoredEntity = ResponseBody<"GET /api/monitored">["entities"][number];
type Reading = NonNullable<MonitoredEntity["state"]>;

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
  entities: [desk, reading, roomGroup, ungroupedSensor],
} satisfies ResponseBody<"GET /api/catalog">;

export const emptySelection = {
  entity_ids: [],
} satisfies ResponseBody<"GET /api/selection">;

export const emptyMonitored = {
  entities: [],
} satisfies Schemas["MonitoredEntities"];

export const health = {
  status: "healthy",
  error: null,
  source_id: "demo",
  checked_at: "2026-10-12T10:00:08+00:00",
} satisfies Health;

/** Health while the reader or Home Assistant cannot be reached. */
export const unreachableHealth = {
  ...health,
  status: "unreachable",
  error: "connection",
} satisfies Health;

/** The 503 body of /api/catalog and selecting while Home Assistant cannot be reached. */
export const unreachableBody = {
  error: "connection",
  message: "Can't reach Home Assistant",
};

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

type ChangeRecord = Schemas["ChangeRecord"];

/** A change Home Assistant recorded, with the ID the API gives it: entity and time. */
const change = (
  fields: Omit<ChangeRecord, "id" | "entity_id" | "source_entity_id" | "device_kind"> &
    Partial<ChangeRecord>,
): ChangeRecord => {
  const entity_id = fields.entity_id ?? "light.hue_desk";
  return {
    device_kind: "light",
    source_entity_id: entity_id,
    ...fields,
    entity_id,
    id: `${entity_id}@${fields.time}`,
  };
};

export const change3 = change({
  time: "2026-10-12T09:59:50Z",
  old: { device_kind: "light", availability: "available", state: "off" },
  new: { device_kind: "light", availability: "available", state: "on", brightness: 150 },
});

export const change2 = change({
  time: "2026-10-12T09:59:40Z",
  old: { device_kind: "light", availability: "available", state: "on" },
  new: { device_kind: "light", availability: "available", state: "off" },
});

export const change1 = change({
  time: "2026-10-12T09:59:30Z",
  old: { device_kind: "light", availability: "available", state: "off" },
  new: { device_kind: "light", availability: "available", state: "on", brightness: 50 },
});

// A different Home Assistant entity ID, so the details show which one changed.
export const colorChange = change({
  source_entity_id: "light.desk_lamp_bulb",
  time: "2026-10-12T09:59:55Z",
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
});

// The smart plug turning on: a switch reading has a state but no light attributes.
export const plugChange = change({
  device_kind: "switch",
  entity_id: smartPlug.id,
  time: "2026-10-12T09:59:58Z",
  old: { device_kind: "switch", availability: "available", state: "off" },
  new: { device_kind: "switch", availability: "available", state: "on" },
});

export const eventsPage1 = {
  events: [change3, change2],
  // Opaque to the dashboard: it only sends it back.
  next_cursor: "opaque-cursor-after-change-2",
} satisfies Schemas["EventsPage"];

export const eventsPage2 = {
  events: [change1],
  next_cursor: null,
} satisfies Schemas["EventsPage"];

export const emptyEvents = {
  events: [],
  next_cursor: null,
} satisfies Schemas["EventsPage"];

export const discoveryRefresh = {
  refresh_revision: 1,
} satisfies ResponseBody<"POST /api/discovery/refresh">;

/** The dashboard's `toLocaleString` rendering of an API timestamp. */
export const localTime = (value: string): string => new Date(value).toLocaleString();

export const controlOff = { state: "off", heartbeat_at: null } satisfies Schemas["ControlStatus"];

export const controlOn = {
  state: "on",
  heartbeat_at: "2026-10-12T10:00:08+00:00",
} satisfies Schemas["ControlStatus"];

const reportedAt = "2026-10-12T10:00:06+00:00";

/** A monitored light in the state Home Assistant reports now. */
export const monitoredLight = (
  entity: Schemas["Entity"],
  state: Reading["state"],
  availability: Reading["availability"] = "available",
  brightness?: number,
) =>
  ({
    ...entity,
    source_entity_id: entity.id,
    state: {
      device_kind: "light",
      state,
      availability,
      last_changed: reportedAt,
      last_updated: reportedAt,
      ...(brightness === undefined ? {} : { brightness }),
    },
    missing: false,
  }) satisfies MonitoredEntity;

/** A monitored plug or switch in the state Home Assistant reports now. */
export const monitoredSwitch = (
  entity: Schemas["Entity"] & { device_kind: "switch" },
  state: Reading["state"],
  availability: Reading["availability"] = "available",
) =>
  ({
    ...entity,
    source_entity_id: entity.id,
    state: {
      device_kind: "switch",
      state,
      availability,
      last_changed: reportedAt,
      last_updated: reportedAt,
    },
    missing: false,
  }) satisfies MonitoredEntity;

/** A monitored device while Home Assistant cannot be reached: listed, with no state. */
export const unknownDevice = (entity: Schemas["Entity"]) =>
  ({
    ...entity,
    name: entity.id,
    device_id: null,
    device_name: null,
    source_entity_id: null,
    state: null,
    missing: false,
  }) satisfies MonitoredEntity;

/** A monitored device Home Assistant no longer has. */
export const missingDevice = (entity: Schemas["Entity"]) =>
  ({
    ...entity,
    source_entity_id: null,
    state: null,
    missing: true,
  }) satisfies MonitoredEntity;

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
