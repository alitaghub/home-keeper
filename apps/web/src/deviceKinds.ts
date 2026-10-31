import type { Schemas } from "./api/client";

export type Kind = NonNullable<Schemas["Entity"]["device_kind"]>;

/** Each device kind's heading on Home and Setup. */
export const HEADINGS = {
  light: "Lights",
  switch: "Plugs and switches",
  on_off: "Other on/off",
  measurement: "Measurements",
  state: "Other",
} as const satisfies Record<Kind, string>;

/** The device kinds in the order Home and Setup list them. */
export const KIND_GROUPS = (
  ["light", "switch", "on_off", "measurement", "state"] as const satisfies readonly Kind[]
).map((kind) => ({ kind, heading: HEADINGS[kind] }));

/** A device's kind; one without a kind counts as a light, as the API does. */
export const kindOf = (entity: Pick<Schemas["Entity"], "device_kind">): Kind =>
  entity.device_kind ?? "light";
