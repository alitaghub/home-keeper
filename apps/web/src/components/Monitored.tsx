import { useEffect, useRef, useState } from "react";
import type { Schemas } from "../api/client";
import { brightnessPercent, date } from "../format";
import type { DeviceUsage, Usage } from "../useUsage";
import { lightName } from "./Catalog";
import { UsageStrip } from "./History";

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
  if (state.state === "on") {
    const brightness = brightnessOf(state);
    return brightness == null ? "On" : `On · ${brightnessPercent(brightness)}%`;
  }
  return state.state === "off" ? "Off" : "Unknown";
}

function StateDetails({ entity, state }: { entity: Schemas["MonitoredEntity"]; state: State }) {
  const brightness = brightnessOf(state);
  return (
    <>
      {brightness != null && <small>{`Brightness ${brightness} / 255`}</small>}
      <small>{`Home Assistant entity: ${entity.source_entity_id ?? entity.id}`}</small>
      <small>{`Last changed: ${date(state.last_changed)}`}</small>
      <small>{`Last updated: ${date(state.last_updated)}`}</small>
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
    <button
      type="button"
      className={action === "off" ? "switch on" : "switch"}
      aria-label={`${label} ${name}`}
      disabled={sending}
      onClick={() => onClick(action)}
    >
      {sending ? "Sending…" : label}
    </button>
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
    <label className="dimmer">
      <input
        type="range"
        min={1}
        max={100}
        value={value}
        disabled={disabled}
        aria-label={`Brightness of ${name}`}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <output>{`${value}%`}</output>
    </label>
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
  const name = lightName(entity);
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
    <article className={recorded === "on" ? "tile lit" : "tile"}>
      <div className="tile-head">
        <h3>{name}</h3>
      </div>
      <p className="tile-state">{stateText(state)}</p>
      {!entity.supported && entity.reason && <p className="tile-note">{entity.reason}</p>}
      {switchable && (
        <div className="tile-controls">
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
        <UsageStrip range={recent.range} device={recent.device} caption="Last 24 hours" />
      )}
      <button
        type="button"
        className="quiet history-link"
        aria-label={`Usage history of ${name}`}
        onClick={() => onHistory(entity)}
      >
        Usage history
      </button>
      <details className="details">
        <summary>Details</summary>
        {state ? (
          <StateDetails entity={entity} state={state} />
        ) : (
          <small>
            {entity.missing
              ? "Home Assistant no longer has this device."
              : "Home Assistant cannot be read right now."}
          </small>
        )}
        <button
          type="button"
          className="quiet"
          aria-label={`Stop monitoring ${name}`}
          onClick={() => onStop(entity.id)}
        >
          Stop monitoring
        </button>
      </details>
    </article>
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
  /** Null while control is off: no light, plug or switch gets a switch. */
  onSwitch: Switch | null;
  onHistory: (entity: Schemas["MonitoredEntity"]) => void;
  onChooseLights: () => void;
  /** IDs of devices whose command is still running. */
  sending: ReadonlySet<string>;
}) {
  const switches = entities?.filter((entity) => entity.device_kind === "switch") ?? [];
  const lights = entities?.filter((entity) => entity.device_kind !== "switch") ?? [];
  const devices = new Map(usage?.devices.map((device) => [device.entity_id, device]));
  const recent = (id: string): Recent | null => usage && { range: usage, device: devices.get(id) };
  return (
    <>
      <div id="monitored" className="tiles">
        {entities && !entities.length && (
          <div className="empty">
            <p>No lights monitored yet. Choose the lights Home Keeper should look after.</p>
            <button type="button" className="primary" onClick={onChooseLights}>
              Choose lights
            </button>
          </div>
        )}
        {lights.map((entity) => (
          <Tile
            key={entity.id}
            entity={entity}
            recent={recent(entity.id)}
            onStop={onStop}
            onSwitch={onSwitch}
            onHistory={onHistory}
            busy={sending.has(entity.id)}
          />
        ))}
      </div>
      {switches.length > 0 && (
        <section className="switches" aria-labelledby="switches-heading">
          <h2 id="switches-heading">Plugs and switches</h2>
          <div id="monitored-switches" className="tiles">
            {switches.map((entity) => (
              <Tile
                key={entity.id}
                entity={entity}
                recent={recent(entity.id)}
                onStop={onStop}
                onSwitch={onSwitch}
                onHistory={onHistory}
                busy={sending.has(entity.id)}
              />
            ))}
          </div>
        </section>
      )}
    </>
  );
}
