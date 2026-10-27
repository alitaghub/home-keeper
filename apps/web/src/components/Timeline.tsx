import type { Schemas } from "../api/client";
import { brightnessPercent, date, time } from "../format";

type LightReading = Schemas["LightReading"];

const changeFields = [
  "state",
  "availability",
  "brightness",
  "color_mode",
  "color_temp_kelvin",
  "hs_color",
  "xy_color",
  "rgb_color",
  "rgbw_color",
  "rgbww_color",
] as const satisfies readonly (keyof LightReading)[];

const colorFields = new Set<string>([
  "color_mode",
  "color_temp_kelvin",
  "hs_color",
  "xy_color",
  "rgb_color",
  "rgbw_color",
  "rgbww_color",
]);

const sourceTimeFields = ["last_changed", "last_updated", "last_reported"] as const;

type Field = (typeof changeFields)[number];
type Value = LightReading[Field];

const valueText = (value: Value) =>
  value == null ? "Not captured" : Array.isArray(value) ? value.join(", ") : String(value);

const label = (field: string) => field.replaceAll("_", " ");

function changedFields(event: Schemas["EventRecord"]): Field[] {
  return changeFields.filter(
    (field) =>
      JSON.stringify(event.old?.[field] ?? null) !== JSON.stringify(event.new?.[field] ?? null),
  );
}

/** One plain sentence for the change, such as "turned on at 40%". */
function summary(event: Schemas["EventRecord"], fields: Field[]) {
  const reading = event.new;
  const percent =
    reading?.brightness == null ? "" : ` at ${brightnessPercent(reading.brightness)}%`;
  if (fields.includes("availability") && reading?.availability !== "available") {
    return reading?.availability === "unavailable" ? "became unavailable" : "state unknown";
  }
  if (fields.includes("state") && reading?.state) {
    return reading.state === "on" ? `turned on${percent}` : `turned ${reading.state}`;
  }
  if (fields.includes("availability")) return "became available";
  if (fields.includes("brightness") && reading?.brightness != null) {
    return `brightness set to ${brightnessPercent(reading.brightness)}%`;
  }
  if (fields.some((field) => colorFields.has(field))) return "color changed";
  return "no changed field captured";
}

export function Timeline({
  events,
  names,
}: {
  events: Schemas["EventRecord"][] | null;
  /** Light names by entity ID; an unknown light shows its Home Assistant entity ID. */
  names: ReadonlyMap<string, string>;
}) {
  return (
    <div id="timeline" className="rows">
      {events && !events.length && (
        <p className="empty">
          No recorded changes in retained history. Select a light and wait for a change. Earlier
          changes may be missing.
        </p>
      )}
      {events?.map((event) => {
        const fields = changedFields(event);
        return (
          <article key={event.id} className="row recorded-event">
            <details>
              <summary>
                <span className="row-main">
                  <strong>
                    {names.get(event.entity_id) ?? (event.source_entity_id || event.entity_id)}
                  </strong>{" "}
                  {summary(event, fields)}
                </span>
                <time dateTime={event.event_time ?? undefined}>{time(event.event_time)}</time>
              </summary>
              <ul className="change-values">
                {fields.length ? (
                  fields.map((field) => (
                    <li key={field}>
                      {`${label(field)}: ${valueText(event.old?.[field])} → ${valueText(event.new?.[field])}`}
                    </li>
                  ))
                ) : (
                  <li>No changed field captured.</li>
                )}
              </ul>
              <small>{`Source: ${event.source_id} · Entity: ${event.source_entity_id || "Not captured"} · Identity: ${event.entity_id}`}</small>
              <small className="evidence-id">{`Evidence ID: ${event.id}`}</small>
              <small>{`Source event: ${date(event.event_time)}`}</small>
              <small>{`Connector received: ${date(event.received_at)} · Stored: ${date(event.persisted_at)}`}</small>
              {sourceTimeFields.map((field) => {
                const value = event.new?.[field];
                return value ? (
                  <small key={field}>{`New state source ${label(field)}: ${date(value)}`}</small>
                ) : null;
              })}
            </details>
          </article>
        );
      })}
    </div>
  );
}
