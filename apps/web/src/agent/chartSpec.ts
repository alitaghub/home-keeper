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

// An ISO 8601 time with a UTC offset, as the agent's schema requires.
const AWARE_TIME = /^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d:?\d\d)$/i;
const isAwareTime = (value: unknown): value is string =>
  typeof value === "string" && AWARE_TIME.test(value) && !Number.isNaN(Date.parse(value));

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
  if (!isAwareTime(start) || !isAwareTime(end) || Date.parse(end) <= Date.parse(start)) {
    return null;
  }
  return { kind: "usage_by_day", entityIds, start, end };
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
