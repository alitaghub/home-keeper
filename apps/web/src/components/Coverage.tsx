import type { Schemas } from "../api/client";
import { date, time } from "../format";

type Gap = Schemas["CoverageGap"];

const gapReasons: Record<Gap["reason"], string> = {
  connection: "Connector connection lost",
  ingestion: "Observations could not be saved",
  authentication: "Authentication failed",
  timeout: "Collection timed out",
  restart: "Application restarted",
  overflow: "Recovery buffer exceeded its limit",
  reconciliation: "Snapshot differed from recorded state",
  heartbeat: "Collector heartbeat missing",
  selection: "Monitoring selection changed",
  invalid_data: "Source data was invalid",
  configuration: "Connector configuration needs attention",
  tls: "Certificate verification failed",
  missing: "Selected entity could not be read",
};

export const openGaps = (coverage: Schemas["CoverageReport"] | null) =>
  coverage?.gaps.filter((gap) => !gap.recovered_at) ?? [];

export function Coverage({ coverage }: { coverage: Schemas["CoverageReport"] | null }) {
  return (
    <>
      <p id="coverage-status" className="history-status" role="status" aria-live="polite">
        {coverage
          ? `Current collection: ${coverage.state}. Last confirmed: ${date(coverage.last_confirmed_at)}. History outside recorded collection windows is unknown.`
          : "Waiting for coverage records."}
      </p>
      <div id="coverage" className="rows">
        {coverage && !coverage.gaps.length && (
          <p className="empty">
            No retained interruption records. This does not prove continuous history.
          </p>
        )}
        {coverage?.gaps.map((gap) => (
          <article
            key={gap.id}
            id={`gap-${gap.id}`}
            className={gap.recovered_at ? "row gap" : "row gap open"}
          >
            <details>
              <summary>
                <span className="row-main">
                  <strong>
                    {gap.recovered_at ? "Collection resumed" : "Collection interrupted"}
                  </strong>{" "}
                  {gapReasons[gap.reason]}
                </span>
                <time dateTime={gap.detected_at}>
                  {gap.recovered_at
                    ? `${time(gap.last_confirmed_at)} – ${time(gap.recovered_at)}`
                    : `since ${time(gap.last_confirmed_at)}`}
                </time>
              </summary>
              <small>{`Possible missing history since: ${date(gap.last_confirmed_at)}. The exact start is unknown.`}</small>
              <small>{`Interruption noticed: ${date(gap.detected_at)}`}</small>
              <small>
                {gap.recovered_at
                  ? `Recovery completed: ${date(gap.recovered_at)}`
                  : "Recovery pending. Current evidence may be stale."}
              </small>
              <small>{`Source: ${gap.source_id} · Entities: ${gap.entity_ids.join(", ") || "None selected"}`}</small>
              <small>{`Gap evidence ID: ${gap.id}`}</small>
            </details>
          </article>
        ))}
      </div>
    </>
  );
}
