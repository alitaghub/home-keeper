// The agent's show_chart spec, parsed from the tool call's arguments. The agent picks the
// kind and the data only; the panel owns every color and size. Names are untrusted text.
// The agent's schema (apps/agent/src/home_keeper_agent/charts.py) accepts and rejects the
// same specs; the shared fixtures in apps/agent/tests/fixtures/chart_specs.json hold both
// parsers to that.

export type Series = { name: string; values: number[] };

export type CategoricalChart = {
  kind: "bar" | "line";
  title: string;
  unit?: string;
  xLabel?: string;
  yLabel?: string;
  categories: string[];
  series: Series[];
};

export type UsageByDayChart = {
  kind: "usage_by_day";
  entityIds: string[];
  start: string;
  end: string;
};

export type ChartSpec = CategoricalChart | UsageByDayChart;

// The panel has this many series colors, as the agent's schema allows.
export const MAX_SERIES = 5;
const MAX_CATEGORIES = 60;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

// Like Pydantic's `extra="forbid"`: a styling field, or any other, makes the spec invalid.
const onlyKeys = (value: Record<string, unknown>, allowed: readonly string[]) =>
  Object.keys(value).every((key) => allowed.includes(key));

// Pydantic counts code points, not UTF-16 units.
const isText = (value: unknown, max = Number.POSITIVE_INFINITY): value is string =>
  typeof value === "string" && value !== "" && [...value].length <= max;

/** Absent or null is undefined; anything but text within `max` is null (invalid). */
const optionalText = (value: unknown, max: number): string | undefined | null =>
  value == null ? undefined : isText(value, max) ? value : null;

const isTexts = (value: unknown, max?: number): value is string[] =>
  Array.isArray(value) && value.every((entry) => isText(entry, max));

const isNumbers = (value: unknown): value is number[] =>
  Array.isArray(value) && value.every((v) => typeof v === "number" && Number.isFinite(v));

// An ISO 8601 time with a UTC offset, in the forms Pydantic's AwareDatetime accepts: a `T`,
// `t`, space or `_` between date and time; optional seconds with a `.` or `,` fraction; and
// `Z`, `z`, `+HH:MM` or `+HHMM`.
const AWARE_TIME =
  /^(?<year>\d{4})-(?<month>\d\d)-(?<day>\d\d)[Tt _](?<hour>\d\d):(?<minute>\d\d)(?::(?<second>\d\d)(?:[.,](?<fraction>\d+))?)?(?:[Zz]|(?<sign>[+-])(?<offsetHour>\d\d):?(?<offsetMinute>\d\d))$/;

/** Milliseconds since the epoch, or null when `value` is not such a time or no real date. */
function awareTime(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const parts = AWARE_TIME.exec(value)?.groups;
  if (!parts) return null;
  const number = (name: string) => Number(parts[name] ?? 0);
  const [year, month, day] = [number("year"), number("month"), number("day")];
  const [hour, minute, second] = [number("hour"), number("minute"), number("second")];
  const [offsetHour, offsetMinute] = [number("offsetHour"), number("offsetMinute")];
  // setUTCFullYear, unlike Date.UTC, keeps years below 100 as they are.
  const monthEnd = new Date(0);
  monthEnd.setUTCFullYear(year, month, 0);
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > monthEnd.getUTCDate()) return null;
  if (hour > 23 || minute > 59 || second > 59 || offsetHour > 23 || offsetMinute > 59) {
    return null;
  }
  const moment = new Date(0);
  moment.setUTCFullYear(year, month - 1, day);
  const milliseconds = Math.floor(Number(`0.${parts.fraction ?? "0"}`) * 1000);
  moment.setUTCHours(hour, minute, second, milliseconds);
  const offset = (parts.sign === "-" ? -1 : 1) * (offsetHour * 60 + offsetMinute) * 60_000;
  return moment.getTime() - offset;
}

// The longest range /api/usage answers, as the agent's schema allows.
const MAX_USAGE_MS = 366 * 24 * 60 * 60 * 1000;

function parseSeries(value: unknown, length: number): Series | null {
  if (!isRecord(value) || !onlyKeys(value, ["name", "values"])) return null;
  if (!isText(value.name, 60) || !isNumbers(value.values)) return null;
  return value.values.length === length ? { name: value.name, values: value.values } : null;
}

const CATEGORICAL_KEYS = ["kind", "title", "unit", "x_label", "y_label", "categories", "series"];

function parseCategorical(spec: Record<string, unknown>, kind: "bar" | "line") {
  if (!onlyKeys(spec, CATEGORICAL_KEYS)) return null;
  const { title, categories, series } = spec;
  const unit = optionalText(spec.unit, 20);
  const xLabel = optionalText(spec.x_label, 60);
  const yLabel = optionalText(spec.y_label, 60);
  if (!isText(title, 120) || unit === null || xLabel === null || yLabel === null) return null;
  if (!isTexts(categories, 40) || !categories.length || categories.length > MAX_CATEGORIES) {
    return null;
  }
  if (!Array.isArray(series) || !series.length || series.length > MAX_SERIES) return null;
  const parsed: Series[] = [];
  for (const entry of series) {
    const one = parseSeries(entry, categories.length);
    if (!one) return null;
    parsed.push(one);
  }
  return {
    kind,
    title,
    categories,
    series: parsed,
    ...(unit && { unit }),
    ...(xLabel && { xLabel }),
    ...(yLabel && { yLabel }),
  } satisfies CategoricalChart;
}

function parseUsageByDay(spec: Record<string, unknown>): UsageByDayChart | null {
  if (!onlyKeys(spec, ["kind", "entity_ids", "start", "end"])) return null;
  const { entity_ids: entityIds, start, end } = spec;
  if (!isTexts(entityIds) || !entityIds.length || entityIds.length > MAX_SERIES) return null;
  const from = awareTime(start);
  const to = awareTime(end);
  if (from === null || to === null || to <= from || to - from > MAX_USAGE_MS) return null;
  // The keeper reads these as plain ISO 8601 in UTC, whichever form the agent wrote.
  return {
    kind: "usage_by_day",
    entityIds,
    start: new Date(from).toISOString(),
    end: new Date(to).toISOString(),
  };
}

/** The chart a show_chart call asks for, or null when its spec does not parse. */
export function parseChartSpec(args: unknown): ChartSpec | null {
  if (!isRecord(args) || !onlyKeys(args, ["spec"]) || !isRecord(args.spec)) return null;
  const { spec } = args;
  switch (spec.kind) {
    case "bar":
    case "line":
      return parseCategorical(spec, spec.kind);
    case "usage_by_day":
      return parseUsageByDay(spec);
    default:
      return null;
  }
}
