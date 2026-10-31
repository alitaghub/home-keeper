// What a chart card shows, built from a parsed spec (and, for usage, the keeper's report):
// the marks to draw, the legend, the table, the CSV and the summary a screen reader hears.
// Every name in here is untrusted text from the agent or Home Assistant; callers render it
// as text only.
import { duration } from "../format";
import type { Usage } from "../useUsage";
import type { CategoricalChart, UsageByDayChart } from "./chartSpec";

/** A color as an SVG fill (a CSS variable) and as a Tailwind class for an HTML swatch. */
type Color = { fill: string; swatch: string };

// The app's five chart colors, one per series; the spec allows no more series than this.
const SERIES_COLORS: readonly Color[] = [
  { fill: "var(--chart-1)", swatch: "bg-chart-1" },
  { fill: "var(--chart-2)", swatch: "bg-chart-2" },
  { fill: "var(--chart-3)", swatch: "bg-chart-3" },
  { fill: "var(--chart-4)", swatch: "bg-chart-4" },
  { fill: "var(--chart-5)", swatch: "bg-chart-5" },
];
const ON: Color = { fill: "var(--status-on)", swatch: "bg-status-on" };
const UNKNOWN: Color = { fill: "var(--status-unknown)", swatch: "bg-status-unknown" };

/** One drawn series, keyed by a fixed id so no name ever becomes a data key. */
export type Mark = {
  key: string;
  label: string;
  fill: string;
  values: readonly number[];
  /** Marks with the same stack draw on top of each other. */
  stack?: string;
};

export type Cell = { text: string; raw: string | number };
export type Table = { columns: readonly string[]; rows: readonly (readonly Cell[])[] };

export type Drawing = {
  kind: "bar" | "line";
  title: string;
  subtitle?: string;
  /** The accessible name: what the chart shows, in a sentence or two. */
  summary: string;
  categories: readonly string[];
  marks: readonly Mark[];
  legend: readonly { label: string; swatch: string }[];
  table: Table;
  /** Totals under a usage chart, one line per device. */
  totals?: readonly { name: string; on: string; unknown: string }[];
};

const valueText = (value: number, unit: string | undefined) =>
  unit ? `${value.toLocaleString()} ${unit}` : value.toLocaleString();

function colorOf(index: number): Color {
  // The parser allows at most as many series as there are colors.
  return SERIES_COLORS[index % SERIES_COLORS.length] ?? ON;
}

function highest(chart: CategoricalChart) {
  let best: { series: string; category: string; value: number } | undefined;
  for (const series of chart.series) {
    series.values.forEach((value, index) => {
      if (best && value <= best.value) return;
      best = { series: series.name, category: chart.categories[index] ?? "", value };
    });
  }
  return best;
}

export function categoricalDrawing(chart: CategoricalChart): Drawing {
  const { categories, series, unit } = chart;
  const marks = series.map((entry, index) => ({
    key: `s${index}`,
    label: entry.name,
    fill: colorOf(index).fill,
    values: entry.values,
  }));
  const top = highest(chart);
  const what = series.length === 1 ? (series[0]?.name ?? "") : `${series.length} series`;
  const summary = [
    `${chart.kind === "bar" ? "Bar" : "Line"} chart: ${chart.title}.`,
    `${what} across ${categories.length === 1 ? "1 category" : `${categories.length} categories`}, from ${categories[0]} to ${categories.at(-1)}.`,
    top && `Highest: ${top.series}, ${top.category}, ${valueText(top.value, unit)}.`,
  ]
    .filter(Boolean)
    .join(" ");
  const subtitle = [chart.yLabel, unit && `in ${unit}`].filter(Boolean).join(", ");
  return {
    kind: chart.kind,
    title: chart.title,
    ...(subtitle && { subtitle }),
    summary,
    categories,
    marks,
    legend: series.map((entry, index) => ({ label: entry.name, swatch: colorOf(index).swatch })),
    table: {
      columns: [chart.xLabel ?? "Category", ...series.map((entry) => entry.name)],
      rows: categories.map((category, row) => [
        { text: category, raw: category },
        ...series.map((entry) => {
          const value = entry.values[row] ?? 0;
          return { text: valueText(value, unit), raw: value };
        }),
      ]),
    },
  };
}

// A usage day is a calendar date in the report's time zone; format it as that date, not as a
// moment in the browser's zone.
const dayLabel = (date: string) =>
  new Date(`${date}T00:00:00Z`).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });

const hours = (seconds: number) => Math.round((seconds / 3600) * 100) / 100;

/** The usage per day of the chart's devices, in its request order, labelled by `nameOf`. */
export function usageDrawing(
  chart: UsageByDayChart,
  usage: Usage,
  nameOf: (entityId: string) => string,
): Drawing {
  const devices = chart.entityIds.flatMap((id) => {
    const device = usage.devices.find((entry) => entry.entity_id === id);
    return device ? [{ device, name: nameOf(id) }] : [];
  });
  const dates = devices[0]?.device.days.map((day) => day.date) ?? [];
  const perDay = (device: Usage["devices"][number]) =>
    dates.map((date) => device.days.find((day) => day.date === date) ?? null);
  // A range Home Assistant recorded nothing for, as after its recorder purged it: every day
  // is unknown.
  const noHistory = devices.every(
    ({ device }) =>
      device.periods.length === 0 &&
      device.unknown_intervals.every((interval) => interval.reason === "no_history"),
  );
  const title = "On and unknown time per day";
  const zone = `Hours per day, in the ${usage.timezone} time zone.`;
  const totals = devices.map(({ device, name }) => ({
    name,
    on: duration(device.on_seconds),
    unknown: duration(device.unknown_seconds),
  }));
  const range = dates.length ? ` from ${dates[0]} to ${dates.at(-1)}` : "";
  return {
    kind: "bar",
    title,
    subtitle: noHistory ? `No history is recorded for this range. ${zone}` : zone,
    summary: `${title}${range}. ${totals
      .map((total) => `${total.name}: on ${total.on}, unknown ${total.unknown}`)
      .join("; ")}.`,
    categories: dates.map(dayLabel),
    // One stack per device: on time under unknown time.
    marks: devices.flatMap(({ device, name }, index) => {
      const days = perDay(device);
      return [
        {
          key: `d${index}on`,
          label: `${name} on`,
          fill: ON.fill,
          stack: `d${index}`,
          values: days.map((day) => hours(day?.on_seconds ?? 0)),
        },
        {
          key: `d${index}unknown`,
          label: `${name} unknown`,
          fill: UNKNOWN.fill,
          stack: `d${index}`,
          values: days.map((day) => hours(day?.unknown_seconds ?? 0)),
        },
      ];
    }),
    legend: [
      { label: "On", swatch: ON.swatch },
      { label: "Unknown", swatch: UNKNOWN.swatch },
    ],
    table: {
      columns: ["Date", ...devices.flatMap(({ name }) => [`${name} on`, `${name} unknown`])],
      rows: dates.map((date, row) => [
        { text: date, raw: date },
        ...devices.flatMap(({ device }) => {
          const day = perDay(device)[row];
          return [day?.on_seconds ?? 0, day?.unknown_seconds ?? 0].map((seconds) => ({
            text: duration(seconds),
            // Hours, for a spreadsheet to add up.
            raw: (seconds / 3600).toFixed(2),
          }));
        }),
      ]),
    },
    totals,
  };
}

// A spreadsheet runs a cell that starts with one of these as a formula.
const FORMULA_START = /^[=+\-@]/;

function csvField(value: string | number): string {
  // Numbers stay raw, negative ones too: a number is never read as a formula.
  const text =
    typeof value === "number" ? String(value) : FORMULA_START.test(value) ? `'${value}` : value;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

/** The table as CSV: a header row, then one line per row, with raw numbers. */
export function tableCsv(table: Table): string {
  const lines = [table.columns, ...table.rows.map((row) => row.map((cell) => cell.raw))];
  return `${lines.map((line) => line.map(csvField).join(",")).join("\r\n")}\r\n`;
}

/** `<title-slug>-<YYYY-MM-DD>.<extension>`, dated today in the browser's zone. */
export function chartFileName(title: string, extension: "png" | "csv", today = new Date()) {
  const slug =
    title
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60)
      .replace(/-+$/, "") || "chart";
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
  return `${slug}-${date}.${extension}`;
}

/** Saves a file the browser made, through a temporary blob link. */
export function saveFile(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  // Revoking at once can cancel the download in some browsers; FileSaver waits 40 s.
  setTimeout(() => URL.revokeObjectURL(url), 40_000);
}
