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

/** Home Assistant brightness (1–255) as the percent a person sets, never below 1%. */
export const brightnessPercent = (brightness: number) =>
  Math.max(1, Math.round((brightness * 100) / 255));

/** Seconds as hours and minutes, such as "5h 12m" or "40m". */
export const duration = (seconds: number) => {
  const minutes = Math.round(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
};
