import type { Schemas } from "./api/client";

export type Kind = NonNullable<Schemas["Entity"]["device_kind"]>;

/** The device kinds, in the order Home and Setup list them, with their headings. */
export const KIND_GROUPS = [
  { kind: "light", heading: "Lights" },
  { kind: "switch", heading: "Plugs and switches" },
  { kind: "on_off", heading: "Other on/off" },
  { kind: "measurement", heading: "Measurements" },
  { kind: "state", heading: "Other" },
] as const satisfies readonly { kind: Kind; heading: string }[];

/** A device's kind; one without a kind counts as a light, as the API does. */
export const kindOf = (entity: Pick<Schemas["Entity"], "device_kind">): Kind =>
  entity.device_kind ?? "light";
