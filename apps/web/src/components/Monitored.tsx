import type { Schemas } from "../api/client";
import { date } from "../format";
import { lightName } from "./Catalog";

type Baseline = NonNullable<Schemas["MonitoredEntity"]["baseline"]>;
export type LightAction = Schemas["CommandRequest"]["command"]["action"];

const kindLabels: Record<Baseline["kind"], string> = {
  baseline: "Initial baseline",
  reconciliation: "Polling reconciliation snapshot",
  event: "Latest recorded event",
  recovery: "Recovery snapshot; transition time unknown",
};

function Evidence({ baseline, confirmedAt }: { baseline: Baseline; confirmedAt: string | null }) {
  const gapIds = baseline.kind === "event" ? null : baseline.coverage_gap_ids;
  return (
    <>
      <small>{kindLabels[baseline.kind]}</small>
      {baseline.brightness != null && <p>{`Brightness ${baseline.brightness} / 255`}</p>}
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
      className="primary"
      aria-label={`${label} ${name}`}
      disabled={sending}
      onClick={() => onClick(action)}
    >
      {sending ? "Sending…" : label}
    </button>
  );
}

export function Monitored({
  entities,
  onStop,
  onSwitch,
  sending,
}: {
  entities: Schemas["MonitoredEntity"][] | null;
  onStop: (id: string) => void;
  /** Null while control is off: no light gets a switch. */
  onSwitch: ((entity: Schemas["MonitoredEntity"], action: LightAction) => void) | null;
  /** IDs of lights whose command is still running. */
  sending: ReadonlySet<string>;
}) {
  return (
    <div id="monitored">
      {entities && !entities.length && (
        <p className="empty">
          No lights selected. Choose a light below to establish its first baseline.
        </p>
      )}
      {entities?.map((entity) => {
        const name = lightName(entity);
        const { baseline } = entity;
        // Only an available light with a known on/off state can be switched.
        const recorded = baseline?.availability === "available" ? baseline.state : null;
        const switchable = onSwitch && (recorded === "on" || recorded === "off");
        return (
          <article key={entity.id} className="observation">
            <div className="section-heading">
              <h3>{name}</h3>
              <span className={`tag ${entity.freshness}`}>{entity.freshness}</span>
              <button
                type="button"
                className="secondary"
                aria-label={`Stop monitoring ${name}`}
                onClick={() => onStop(entity.id)}
              >
                Stop monitoring
              </button>
            </div>
            <div className="state-row">
              <p className="state">
                {baseline
                  ? `${baseline.state} · ${baseline.availability}`
                  : "Pending · state unknown"}
              </p>
              {switchable && (
                <SwitchButton
                  name={name}
                  action={recorded === "on" ? "off" : "on"}
                  sending={sending.has(entity.id)}
                  onClick={(action) => onSwitch(entity, action)}
                />
              )}
            </div>
            {baseline ? (
              <Evidence baseline={baseline} confirmedAt={entity.confirmed_at} />
            ) : (
              <p>The collector has not confirmed a selected observation yet.</p>
            )}
          </article>
        );
      })}
    </div>
  );
}
