export const date = (value: string | null | undefined) =>
  value ? new Date(value).toLocaleString() : "Not captured";

/** A short time for today, with the date for any other day. */
export const time = (value: string | null | undefined) => {
  if (!value) return "—";
  const moment = new Date(value);
  const clock = moment.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return moment.toDateString() === new Date().toDateString()
    ? clock
    : `${moment.toLocaleDateString([], { month: "short", day: "numeric" })}, ${clock}`;
};

const relativeUnits = [
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
] as const;

/** How long ago a time was, such as "5 minutes ago", in the browser's language. */
export const ago = (value: string, now = Date.now()) => {
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const seconds = (Date.parse(value) - now) / 1000;
  for (const [unit, size] of relativeUnits) {
    if (Math.abs(seconds) >= size) return format.format(Math.round(seconds / size), unit);
  }
  return format.format(Math.round(seconds), "second");
};

/** Home Assistant brightness (1–255) as the percent a person sets, never below 1%. */
export const brightnessPercent = (brightness: number) =>
  Math.max(1, Math.round((brightness * 100) / 255));

/** Seconds as hours and minutes, such as "5h 12m" or "40m". */
export const duration = (seconds: number) => {
  const minutes = Math.round(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
};

const measured = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

/** A measured value with its unit, such as "412 W"; "—" when there is no value. */
export const measurement = (value: number | null | undefined, unit: string | null | undefined) =>
  value == null ? "—" : unit ? `${measured.format(value)} ${unit}` : measured.format(value);
