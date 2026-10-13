import type { Schemas } from "../api/client";
import { date } from "../format";

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

const sourceTimeFields = ["last_changed", "last_updated", "last_reported"] as const;

type Value = LightReading[(typeof changeFields)[number]];

const valueText = (value: Value) =>
  value == null ? "Not captured" : Array.isArray(value) ? value.join(", ") : String(value);

const label = (field: string) => field.replaceAll("_", " ");

function changes(event: Schemas["EventRecord"]) {
  return changeFields.flatMap((field) => {
    const before = event.old?.[field];
    const after = event.new?.[field];
    return JSON.stringify(before ?? null) === JSON.stringify(after ?? null)
      ? []
      : [`${label(field)}: ${valueText(before)} → ${valueText(after)}`];
  });
}

export function Timeline({ events }: { events: Schemas["EventRecord"][] | null }) {
  return (
    <div id="timeline">
      {events && !events.length && (
        <p className="empty">
          No recorded changes in retained history. Select a light and wait for a change. Earlier
          changes may be missing.
        </p>
      )}
      {events?.map((event) => {
        const changed = changes(event);
        return (
          <article key={event.id} className="observation recorded-event">
            <h3>{event.source_entity_id || event.entity_id}</h3>
            <ul className="change-values">
              {changed.length ? (
                changed.map((text) => <li key={text}>{text}</li>)
              ) : (
                <li>No changed field captured.</li>
              )}
            </ul>
            <small>{`Source: ${event.source_id} · Entity identity: ${event.entity_id}`}</small>
            <small className="evidence-id">{`Evidence ID: ${event.id}`}</small>
            <small>{`Source event: ${date(event.event_time)}`}</small>
            <small>{`Connector received: ${date(event.received_at)} · Stored: ${date(event.persisted_at)}`}</small>
            {sourceTimeFields.map((field) => {
              const value = event.new?.[field];
              return value ? (
                <small key={field}>{`New state source ${label(field)}: ${date(value)}`}</small>
              ) : null;
            })}
          </article>
        );
      })}
    </div>
  );
}
