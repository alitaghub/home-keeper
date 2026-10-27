import { useEffect, useRef, useState } from "react";
import type { Schemas } from "../api/client";
import { brightnessPercent, date } from "../format";
import { lightName } from "./Catalog";

type Baseline = NonNullable<Schemas["MonitoredEntity"]["baseline"]>;
export type LightCommand = Schemas["CommandRequest"]["command"];
export type LightAction = LightCommand["action"];

// A slider sends one command once it stops moving, not one per step.
const DIMMER_SETTLE_MS = 400;

const kindLabels: Record<Baseline["kind"], string> = {
  baseline: "Initial baseline",
  reconciliation: "Polling reconciliation snapshot",
  event: "Latest recorded event",
  recovery: "Recovery snapshot; transition time unknown",
};

const freshnessLabels: Record<Schemas["MonitoredEntity"]["freshness"], string> = {
  fresh: "fresh",
  stale: "stale",
  pending: "waiting",
};

function stateText(baseline: Baseline | null) {
  if (!baseline) return "Waiting for a first reading";
  if (baseline.availability !== "available") {
    return baseline.availability === "unavailable" ? "Unavailable" : "Unknown";
  }
  if (baseline.state === "on") {
    return baseline.brightness == null ? "On" : `On · ${brightnessPercent(baseline.brightness)}%`;
  }
  return baseline.state === "off" ? "Off" : "Unknown";
}

function Evidence({ baseline, confirmedAt }: { baseline: Baseline; confirmedAt: string | null }) {
  const gapIds = baseline.kind === "event" ? null : baseline.coverage_gap_ids;
  return (
    <>
      <small>{kindLabels[baseline.kind]}</small>
      {baseline.brightness != null && <small>{`Brightness ${baseline.brightness} / 255`}</small>}
      {gapIds?.length ? (
        <small>{`Related gap evidence: ${gapIds.join(", ")}. Changes during this gap have unknown timing.`}</small>
      ) : null}
      <small>{`Source: ${baseline.source_id} · Evidence ID: ${baseline.id}`}</small>
      <small>{`Latest confirmed collection: ${date(confirmedAt)}`}</small>
      <small>{`Evidence received: ${date(baseline.received_at)} · Stored: ${date(baseline.persisted_at)}`}</small>
      <small>{`Source last changed: ${date(baseline.last_changed)}`}</small>
    </>
  );
}

// Shows the recorded state's opposite, never the requested one: the label changes
// only once the reload after the command records the light's new state.
function SwitchButton({
  name,
  action,
  sending,
  onClick,
}: {
  name: string;
  action: LightAction;
  sending: boolean;
  onClick: (action: LightAction) => void;
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
  percent,
  disabled,
  onCommit,
}: {
  name: string;
  /** The recorded brightness; the slider returns to it after each command. */
  percent: number;
  disabled: boolean;
  onCommit: (percent: number) => void;
}) {
  const [draft, setDraft] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const shown = draft ?? percent;
  return (
    <label className="dimmer">
      <input
        type="range"
        min={1}
        max={100}
        value={shown}
        disabled={disabled}
        aria-label={`Brightness of ${name}`}
        onChange={(event) => {
          const value = Number(event.target.value);
          setDraft(value);
          clearTimeout(timer.current);
          timer.current = setTimeout(() => {
            setDraft(null);
            onCommit(value);
          }, DIMMER_SETTLE_MS);
        }}
      />
      <output>{`${shown}%`}</output>
    </label>
  );
}

export function Monitored({
  entities,
  onStop,
  onSwitch,
  onChooseLights,
  sending,
}: {
  entities: Schemas["MonitoredEntity"][] | null;
  onStop: (id: string) => void;
  /** Null while control is off: no light gets a switch. */
  onSwitch: ((entity: Schemas["MonitoredEntity"], command: LightCommand) => void) | null;
  onChooseLights: () => void;
  /** IDs of lights whose command is still running. */
  sending: ReadonlySet<string>;
}) {
  return (
    <div id="monitored" className="tiles">
      {entities && !entities.length && (
        <div className="empty">
          <p>No lights monitored yet. Choose the lights Home Keeper should record.</p>
          <button type="button" className="primary" onClick={onChooseLights}>
            Choose lights
          </button>
        </div>
      )}
      {entities?.map((entity) => {
        const name = lightName(entity);
        const { baseline } = entity;
        // Only an available light with a known on/off state can be switched.
        const recorded = baseline?.availability === "available" ? baseline.state : null;
        const switchable = onSwitch && (recorded === "on" || recorded === "off");
        const busy = sending.has(entity.id);
        const percent =
          recorded === "on" && baseline?.brightness != null
            ? brightnessPercent(baseline.brightness)
            : 100;
        return (
          <article key={entity.id} className={recorded === "on" ? "tile lit" : "tile"}>
            <div className="tile-head">
              <h3>{name}</h3>
              {entity.freshness !== "fresh" && (
                <span className={`tag ${entity.freshness}`}>
                  {freshnessLabels[entity.freshness]}
                </span>
              )}
            </div>
            <p className="tile-state">{stateText(baseline)}</p>
            {!entity.supported && entity.reason && <p className="tile-note">{entity.reason}</p>}
            {switchable && (
              <div className="tile-controls">
                <SwitchButton
                  name={name}
                  action={recorded === "on" ? "off" : "on"}
                  sending={busy}
                  onClick={(action) => onSwitch(entity, { action })}
                />
                <Dimmer
                  name={name}
                  percent={percent}
                  disabled={busy}
                  onCommit={(brightness_pct) => onSwitch(entity, { action: "on", brightness_pct })}
                />
              </div>
            )}
            <details className="details">
              <summary>Details</summary>
              {baseline ? (
                <Evidence baseline={baseline} confirmedAt={entity.confirmed_at} />
              ) : (
                <small>The collector has not confirmed a selected observation yet.</small>
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
      })}
    </div>
  );
}
