import type { Schemas } from "../api/client";
import { brightnessPercent, date, time } from "../format";

type ChangeRecord = Schemas["ChangeRecord"];
type LightReading = Schemas["LightReading"];
type Reading = ChangeRecord["new"];

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

type Field = (typeof changeFields)[number];
type Value = LightReading[Field];

/** A field of an event's state. A plug or switch has no light attributes to show. */
function fieldValue(reading: Reading, field: Field): Value {
  if (reading.device_kind === "light") return reading[field];
  return field === "state" || field === "availability" ? reading[field] : undefined;
}

const valueText = (value: Value) =>
  value == null ? "Not captured" : Array.isArray(value) ? value.join(", ") : String(value);

const label = (field: string) => field.replaceAll("_", " ");

function changedFields(event: ChangeRecord): Field[] {
  return changeFields.filter(
    (field) =>
      JSON.stringify(fieldValue(event.old, field) ?? null) !==
      JSON.stringify(fieldValue(event.new, field) ?? null),
  );
}

/** One plain sentence for the change, such as "turned on at 40%". */
function summary(event: ChangeRecord, fields: Field[]) {
  const reading = event.new;
  const brightness = fieldValue(reading, "brightness");
  const percent = typeof brightness === "number" ? ` at ${brightnessPercent(brightness)}%` : "";
  if (fields.includes("availability") && reading.availability !== "available") {
    return reading.availability === "unavailable" ? "became unavailable" : "state unknown";
  }
  if (fields.includes("state")) {
    return reading.state === "on" ? `turned on${percent}` : `turned ${reading.state}`;
  }
  if (fields.includes("availability")) return "became available";
  if (fields.includes("brightness") && typeof brightness === "number") {
    return `brightness set to ${brightnessPercent(brightness)}%`;
  }
  if (fields.some((field) => colorFields.has(field))) return "color changed";
  return "no changed field captured";
}

export function Timeline({
  events,
  names,
}: {
  events: ChangeRecord[] | null;
  /** Device names by entity ID; an unknown device shows its Home Assistant entity ID. */
  names: ReadonlyMap<string, string>;
}) {
  return (
    <div id="timeline" className="rows">
      {events && !events.length && (
        <p className="empty">
          Home Assistant has no changes of monitored devices in the last 30 days. Select a light and
          wait for a change.
        </p>
      )}
      {events?.map((event) => {
        const fields = changedFields(event);
        return (
          <article key={event.id} className="row recorded-event">
            <details>
              <summary>
                <span className="row-main">
                  <strong>{names.get(event.entity_id) ?? event.source_entity_id}</strong>{" "}
                  {summary(event, fields)}
                </span>
                <time dateTime={event.time}>{time(event.time)}</time>
              </summary>
              <ul className="change-values">
                {fields.length ? (
                  fields.map((field) => (
                    <li key={field}>
                      {`${label(field)}: ${valueText(fieldValue(event.old, field))} → ${valueText(fieldValue(event.new, field))}`}
                    </li>
                  ))
                ) : (
                  <li>No changed field captured.</li>
                )}
              </ul>
              <small>{`Entity: ${event.source_entity_id} · Identity: ${event.entity_id}`}</small>
              <small>{`Home Assistant time: ${date(event.time)}`}</small>
            </details>
          </article>
        );
      })}
    </div>
  );
}
