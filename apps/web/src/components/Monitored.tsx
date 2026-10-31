import { ChevronDown, History, Power } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import type { Schemas } from "../api/client";
import { ago, brightnessPercent, date } from "../format";
import { cn } from "../lib/utils";
import type { DeviceUsage, Usage } from "../useUsage";
import { deviceName } from "./Catalog";
import { UsageStrip } from "./History";
import { Button } from "./ui/button";
import { Card } from "./ui/card";

type State = NonNullable<Schemas["MonitoredEntity"]["state"]>;
export type DeviceCommand = Schemas["CommandRequest"]["command"];
export type DeviceAction = DeviceCommand["action"];

// A slider sends one command once it stops moving, not one per step.
const DIMMER_SETTLE_MS = 400;

/** A light's brightness; plugs and switches have none. */
const brightnessOf = (state: State | null) =>
  state?.device_kind === "light" ? (state.brightness ?? null) : null;

function stateText(state: State | null) {
  if (!state) return "State unknown";
  if (state.availability !== "available") {
    return state.availability === "unavailable" ? "Unavailable" : "Unknown";
  }
  // Home Assistant's word for an on/off device's state, such as "Open" for a door.
  if (state.device_kind === "on_off") return state.label;
  if (state.state === "on") {
    const brightness = brightnessOf(state);
    return brightness == null ? "On" : `On · ${brightnessPercent(brightness)}%`;
  }
  return state.state === "off" ? "Off" : "Unknown";
}

type Status = "on" | "off" | "unknown";

/** Only an available device's recorded on or off counts; anything else is unknown. */
function statusOf(state: State | null): Status {
  if (state?.availability !== "available") return "unknown";
  return state.state === "on" || state.state === "off" ? state.state : "unknown";
}

// Three looks that differ in fill, border and text, so off never reads as unknown.
const statusStyles: Record<Status, { pill: string; dot: string }> = {
  on: {
    pill: "border-status-on bg-status-on-muted text-status-on-foreground",
    dot: "bg-status-on",
  },
  off: { pill: "border-border bg-muted text-foreground", dot: "bg-muted-foreground" },
  unknown: {
    pill: "border-dashed border-status-unknown bg-transparent text-muted-foreground",
    dot: "border border-status-unknown bg-transparent",
  },
};

function StatusPill({ state }: { state: State | null }) {
  const status = statusOf(state);
  return (
    <p
      data-status={status}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-sm font-medium whitespace-nowrap",
        statusStyles[status].pill,
      )}
    >
      <span className={cn("size-2 rounded-full", statusStyles[status].dot)} aria-hidden="true" />
      {stateText(state)}
    </p>
  );
}

function StateDetails({ entity, state }: { entity: Schemas["MonitoredEntity"]; state: State }) {
  const brightness = brightnessOf(state);
  return (
    <>
      {brightness != null && <small className="block">{`Brightness ${brightness} / 255`}</small>}
      <small className="block break-all">{`Home Assistant entity: ${entity.source_entity_id ?? entity.id}`}</small>
      <small className="block">{`Last changed: ${date(state.last_changed)}`}</small>
      <small className="block">{`Last updated: ${date(state.last_updated)}`}</small>
    </>
  );
}

// Shows the current state's opposite, never the requested one: the label changes
// only once the reload after the command reads the device's new state.
function SwitchButton({
  name,
  action,
  sending,
  onClick,
}: {
  name: string;
  action: DeviceAction;
  sending: boolean;
  onClick: (action: DeviceAction) => void;
}) {
  const label = `Turn ${action}`;
  return (
    <Button
      type="button"
      variant={action === "off" ? "default" : "outline"}
      className="w-full"
      aria-label={`${label} ${name}`}
      disabled={sending}
      onClick={() => onClick(action)}
    >
      <Power aria-hidden="true" />
      {sending ? "Sending…" : label}
    </Button>
  );
}

function Dimmer({
  name,
  value,
  disabled,
  onChange,
}: {
  name: string;
  value: number;
  disabled: boolean;
  onChange: (percent: number) => void;
}) {
  return (
    <label className="flex items-center gap-3">
      <input
        type="range"
        className="h-1.5 w-full min-w-0 cursor-pointer appearance-none rounded-full bg-muted outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 [&::-moz-range-thumb]:size-4 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border [&::-moz-range-thumb]:border-primary [&::-moz-range-thumb]:bg-background [&::-moz-range-thumb]:shadow-sm [&::-webkit-slider-thumb]:size-4 [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:border [&::-webkit-slider-thumb]:border-primary [&::-webkit-slider-thumb]:bg-background [&::-webkit-slider-thumb]:shadow-sm"
        min={1}
        max={100}
        value={value}
        disabled={disabled}
        aria-label={`Brightness of ${name}`}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <output className="w-10 shrink-0 text-right text-sm text-muted-foreground tabular-nums">{`${value}%`}</output>
    </label>
  );
}

function DeviceDetails({
  entity,
  name,
  onStop,
}: {
  entity: Schemas["MonitoredEntity"];
  name: string;
  onStop: (id: string) => void;
}) {
  const { state } = entity;
  return (
    <details className="group rounded-md border px-3 py-2 text-muted-foreground">
      <summary className="flex cursor-pointer list-none items-center justify-between text-sm font-medium text-foreground [&::-webkit-details-marker]:hidden">
        Details
        <ChevronDown
          className="size-4 transition-transform group-open:rotate-180"
          aria-hidden="true"
        />
      </summary>
      <div className="mt-2 grid gap-1 text-xs">
        {state ? (
          <StateDetails entity={entity} state={state} />
        ) : (
          <small className="block">
            {entity.missing
              ? "Home Assistant no longer has this device."
              : "Home Assistant cannot be read right now."}
          </small>
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="mt-2 justify-self-start"
          aria-label={`Stop monitoring ${name}`}
          onClick={() => onStop(entity.id)}
        >
          Stop monitoring
        </Button>
      </div>
    </details>
  );
}

type Switch = (entity: Schemas["MonitoredEntity"], command: DeviceCommand) => void;

type Recent = { range: Pick<Usage, "start" | "end">; device: DeviceUsage | undefined };

function Tile({
  entity,
  recent,
  onStop,
  onSwitch,
  onHistory,
  busy,
}: {
  entity: Schemas["MonitoredEntity"];
  recent: Recent | null;
  onStop: (id: string) => void;
  onSwitch: Switch | null;
  onHistory: (entity: Schemas["MonitoredEntity"]) => void;
  busy: boolean;
}) {
  const name = deviceName(entity);
  const { state } = entity;
  // Only an available device with a known on/off state can be switched.
  const recorded = state?.availability === "available" ? state.state : null;
  const switchable = onSwitch !== null && (recorded === "on" || recorded === "off");
  // Plugs and switches are on or off, with no brightness.
  const dimmable = entity.device_kind !== "switch";
  const brightness = brightnessOf(state);
  const percent = recorded === "on" && brightness != null ? brightnessPercent(brightness) : 100;
  // The slider's value while it moves; null shows the current brightness.
  const [draft, setDraft] = useState<number | null>(null);
  const settle = useRef<ReturnType<typeof setTimeout>>(undefined);
  const cancelDraft = () => {
    clearTimeout(settle.current);
    setDraft(null);
  };
  useEffect(() => () => clearTimeout(settle.current), []);
  // A pending brightness must not fire once the light can no longer be switched.
  useEffect(() => {
    if (!switchable) clearTimeout(settle.current);
  }, [switchable]);

  return (
    <article className="min-w-0">
      <Card
        className={cn(
          "h-full gap-4 px-5 py-5",
          recorded === "on" && "border-status-on/70 bg-status-on-muted/40",
        )}
      >
        <div className="flex items-start justify-between gap-3">
          <h3 className="min-w-0 font-semibold leading-snug break-words">{name}</h3>
          <StatusPill state={state} />
        </div>
        {!entity.supported && entity.reason && (
          <p className="text-xs text-warning-foreground">{entity.reason}</p>
        )}
        {switchable && (
          <div className="grid gap-3">
            <SwitchButton
              name={name}
              action={recorded === "on" ? "off" : "on"}
              sending={busy}
              onClick={(action) => {
                // The click wins over a slider move that has not settled yet.
                cancelDraft();
                onSwitch(entity, { action });
              }}
            />
            {dimmable && (
              <Dimmer
                name={name}
                value={draft ?? percent}
                disabled={busy}
                onChange={(brightness_pct) => {
                  setDraft(brightness_pct);
                  clearTimeout(settle.current);
                  settle.current = setTimeout(() => {
                    setDraft(null);
                    onSwitch(entity, { action: "on", brightness_pct });
                  }, DIMMER_SETTLE_MS);
                }}
              />
            )}
          </div>
        )}
        {recent?.device && (
          <div className="grid gap-1">
            <span className="text-xs text-muted-foreground" aria-hidden="true">
              Last 24 hours
            </span>
            <UsageStrip range={recent.range} device={recent.device} caption="Last 24 hours" />
          </div>
        )}
        <div className="mt-auto grid gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="justify-self-start px-2"
            aria-label={`Usage history of ${name}`}
            onClick={() => onHistory(entity)}
          >
            <History aria-hidden="true" />
            Usage history
          </Button>
          <DeviceDetails entity={entity} name={name} onStop={onStop} />
        </div>
      </Card>
    </article>
  );
}

/**
 * A device that only reports a state, such as a thermostat or a cover: Home Assistant's word
 * for its state and when it last changed. It cannot be switched and has no usage.
 */
function OtherTile({
  entity,
  onStop,
}: {
  entity: Schemas["MonitoredEntity"];
  onStop: (id: string) => void;
}) {
  const name = deviceName(entity);
  const { state } = entity;
  return (
    <article className="min-w-0">
      <Card className="h-full gap-4 px-5 py-5">
        <div className="flex items-start justify-between gap-3">
          <h3 className="min-w-0 font-semibold leading-snug break-words">{name}</h3>
          {/* Home Assistant has no per-domain word for unavailable or unknown, so those show
              the same badge as a light's or plug's. */}
          {state?.availability === "available" ? (
            <p
              className={cn(
                "inline-flex min-w-0 items-center rounded-full border px-2.5 py-0.5 text-sm font-medium break-words",
                statusStyles.off.pill,
              )}
            >
              {state.label}
            </p>
          ) : (
            <StatusPill state={state} />
          )}
        </div>
        {!entity.supported && entity.reason && (
          <p className="text-xs text-warning-foreground">{entity.reason}</p>
        )}
        {state?.last_changed && (
          <p className="text-sm text-muted-foreground">{`Changed ${ago(state.last_changed)}`}</p>
        )}
        <div className="mt-auto grid gap-2">
          <DeviceDetails entity={entity} name={name} onStop={onStop} />
        </div>
      </Card>
    </article>
  );
}

const grid = "grid gap-4 sm:grid-cols-2 lg:grid-cols-3";
const sectionHeading = "text-lg font-semibold tracking-tight";

/** A headed group of cards on Home; a kind with no monitored device shows no section. */
function Section({
  heading,
  headingId,
  gridId,
  children,
}: {
  heading: string;
  headingId: string;
  gridId: string;
  children: ReactNode[];
}) {
  if (!children.length) return null;
  return (
    <section className="grid gap-4" aria-labelledby={headingId}>
      <h2 id={headingId} className={sectionHeading}>
        {heading}
      </h2>
      <div id={gridId} className={grid}>
        {children}
      </div>
    </section>
  );
}

export function Monitored({
  entities,
  usage,
  onStop,
  onSwitch,
  onHistory,
  onChooseLights,
  sending,
}: {
  entities: Schemas["MonitoredEntity"][] | null;
  /** The last day's usage, for each card's strip; null until it loads. */
  usage: Usage | null;
  onStop: (id: string) => void;
  /** Null while control is off: no light, plug or switch gets a switch. Other devices, on/off
   * ones included, never do. */
  onSwitch: Switch | null;
  onHistory: (entity: Schemas["MonitoredEntity"]) => void;
  onChooseLights: () => void;
  /** IDs of devices whose command is still running. */
  sending: ReadonlySet<string>;
}) {
  const all = entities ?? [];
  const empty = entities !== null && !entities.length;
  const switches = all.filter((entity) => entity.device_kind === "switch");
  const onOffs = all.filter((entity) => entity.device_kind === "on_off");
  const others = all.filter((entity) => entity.device_kind === "state");
  // A device without a kind counts as a light, as the API does.
  const lights = all.filter(
    (entity) =>
      entity.device_kind !== "switch" &&
      entity.device_kind !== "on_off" &&
      entity.device_kind !== "state",
  );
  const devices = new Map(usage?.devices.map((device) => [device.entity_id, device]));
  const recent = (id: string): Recent | null => usage && { range: usage, device: devices.get(id) };
  // An on/off device, such as a fan or a door sensor, has a plug's card without a switch.
  const tile = (entity: Schemas["MonitoredEntity"]) => (
    <Tile
      key={entity.id}
      entity={entity}
      recent={recent(entity.id)}
      onStop={onStop}
      onSwitch={entity.device_kind === "on_off" ? null : onSwitch}
      onHistory={onHistory}
      busy={sending.has(entity.id)}
    />
  );
  return (
    <>
      {/* The lights grid stays mounted, hidden while it has nothing to show, so it keeps its
          place while the list loads and when the last device stops being monitored. */}
      <section
        className="grid gap-4"
        aria-labelledby={lights.length ? "lights-section-heading" : undefined}
        hidden={!lights.length && !empty}
      >
        {lights.length > 0 && (
          <h2 id="lights-section-heading" className={sectionHeading}>
            Lights
          </h2>
        )}
        <div id="monitored" className={grid}>
          {empty && (
            <div className="col-span-full grid justify-items-start gap-3 rounded-xl border border-dashed p-6 text-sm text-muted-foreground">
              <p>No devices monitored yet. Choose the devices Home Keeper should look after.</p>
              <Button type="button" onClick={onChooseLights}>
                Choose devices
              </Button>
            </div>
          )}
          {lights.map(tile)}
        </div>
      </section>
      <Section
        heading="Plugs and switches"
        headingId="switches-heading"
        gridId="monitored-switches"
      >
        {switches.map(tile)}
      </Section>
      <Section heading="Other on/off" headingId="onoff-heading" gridId="monitored-onoff">
        {onOffs.map(tile)}
      </Section>
      <Section heading="Other" headingId="other-heading" gridId="monitored-other">
        {others.map((entity) => (
          <OtherTile key={entity.id} entity={entity} onStop={onStop} />
        ))}
      </Section>
    </>
  );
}
