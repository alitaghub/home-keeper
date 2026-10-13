import type { Schemas } from "../api/client";
import { date } from "../format";
import { lightName } from "./Catalog";

type Baseline = NonNullable<Schemas["MonitoredEntity"]["baseline"]>;

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

export function Monitored({
  entities,
  onStop,
}: {
  entities: Schemas["MonitoredEntity"][] | null;
  onStop: (id: string) => void;
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
            <p className="state">
              {baseline
                ? `${baseline.state} · ${baseline.availability}`
                : "Pending · state unknown"}
            </p>
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
