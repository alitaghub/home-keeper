import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { measurement, time } from "../format";
import { cn } from "../lib/utils";
import { fetchMeasurements, type Measurements, type Series } from "../useMeasurements";
import { type Days, RANGES } from "./History";
import { Button } from "./ui/button";

type Bucket = Series["buckets"][number];
type Range = Pick<Measurements, "start" | "end">;

const WIDTH = 1000;
const SPARK_HEIGHT = 32;
const CHART_HEIGHT = 160;
// Room above and below the drawn values, so the highest and lowest stay visible.
const PAD = 4;

/** Buckets that follow each other without a gap; a missing period starts a new run. */
function runs(buckets: Bucket[]) {
  const found: Bucket[][] = [];
  for (const bucket of buckets) {
    const run = found.at(-1);
    const last = run?.at(-1);
    if (run && last && Date.parse(bucket.start) <= Date.parse(last.end)) run.push(bucket);
    else found.push([bucket]);
  }
  return found;
}

/** Horizontal position of a time across the range, and vertical position of a value from
 * `low` (bottom) to `high` (top); a flat series draws through the middle. */
function axes(range: Range, low: number, high: number, height: number) {
  const from = Date.parse(range.start);
  const span = Date.parse(range.end) - from || 1;
  const x = (moment: string) => ((Date.parse(moment) - from) / span) * WIDTH;
  const y = (value: number) =>
    high > low ? PAD + ((high - value) / (high - low)) * (height - 2 * PAD) : height / 2;
  return { x, y };
}

/** Points of a step line through each bucket's value, from its start to its end. */
function steps(run: Bucket[], value: (bucket: Bucket) => number, axis: ReturnType<typeof axes>) {
  return run.flatMap((bucket) => [
    [axis.x(bucket.start), axis.y(value(bucket))],
    [axis.x(bucket.end), axis.y(value(bucket))],
  ]);
}

const points = (list: number[][]) => list.map(([x, y]) => `${x},${y}`).join(" ");

/** The mean of each bucket as a line across the range; gaps break the line. */
export function Sparkline({
  range,
  series,
  caption,
}: {
  range: Range;
  series: Series;
  caption: string;
}) {
  const means = series.buckets.map((bucket) => bucket.mean);
  const axis = axes(range, Math.min(...means), Math.max(...means), SPARK_HEIGHT);
  const label =
    series.min == null || series.max == null
      ? `${caption}: no statistics`
      : `${caption}: ${measurement(series.min, null)}–${measurement(series.max, series.unit)}`;
  return (
    <svg
      className="block h-8 w-full overflow-hidden rounded-sm"
      viewBox={`0 0 ${WIDTH} ${SPARK_HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
    >
      <rect className="fill-muted" width={WIDTH} height={SPARK_HEIGHT} />
      {runs(series.buckets).map((run) => (
        <polyline
          key={run[0]?.start}
          data-segment="mean"
          className="fill-none stroke-status-on"
          strokeWidth={2}
          vectorEffect="non-scaling-stroke"
          points={points(steps(run, (bucket) => bucket.mean, axis))}
        />
      ))}
    </svg>
  );
}

/** Each bucket's lowest to highest value as a band, and its mean as a line; gaps stay empty. */
function Chart({ range, series }: { range: Range; series: Series }) {
  const axis = axes(range, series.min ?? 0, series.max ?? 0, CHART_HEIGHT);
  return (
    <svg
      className="block h-40 w-full overflow-hidden rounded-sm"
      viewBox={`0 0 ${WIDTH} ${CHART_HEIGHT}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <rect className="fill-muted" width={WIDTH} height={CHART_HEIGHT} />
      {runs(series.buckets).map((run) => (
        <g key={run[0]?.start}>
          <polygon
            data-segment="band"
            className="fill-status-on/30"
            points={points([
              ...steps(run, (bucket) => bucket.max, axis),
              ...steps(run, (bucket) => bucket.min, axis).reverse(),
            ])}
          />
          <polyline
            data-segment="mean"
            className="fill-none stroke-status-on-foreground"
            strokeWidth={2}
            vectorEffect="non-scaling-stroke"
            points={points(steps(run, (bucket) => bucket.mean, axis))}
          />
        </g>
      ))}
    </svg>
  );
}

const total = "rounded-md border px-3 py-2";
const totalLabel = "text-xs text-muted-foreground";
const totalValue = "text-lg font-semibold tabular-nums";
const legend = "inline-flex items-center gap-1.5";
const swatch = "inline-block h-2.5 w-4 rounded-sm";

function Totals({ series }: { series: Series }) {
  const totals = [
    ["Min", series.min],
    ["Mean", series.mean],
    ["Max", series.max],
  ] as const;
  return (
    <dl className="grid grid-cols-3 gap-2">
      {totals.map(([label, value]) => (
        <div key={label} className={total}>
          <dt className={totalLabel}>{label}</dt>
          <dd className={totalValue}>{measurement(value, series.unit)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** One measurement's statistics over a chosen range, in a modal dialog. */
export function MeasurementDialog({
  entityId,
  name,
  onClose,
}: {
  entityId: string;
  name: string;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  // A retry asks again for the same range.
  const [request, setRequest] = useState({ days: 1 as Days, attempt: 0 });
  const { days } = request;
  // Each answer remembers its request, so a range switch or a retry never shows old numbers.
  const [loaded, setLoaded] = useState<
    { request: typeof request; report: Measurements } | { request: typeof request; failed: true }
  >();

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  useEffect(() => {
    let current = true;
    fetchMeasurements(request.days, [entityId]).then(
      (report) => current && setLoaded({ request, report }),
      () => current && setLoaded({ request, failed: true }),
    );
    return () => {
      current = false;
    };
  }, [request, entityId]);

  const shown = loaded?.request === request ? loaded : undefined;
  const report = shown && "report" in shown ? shown.report : undefined;
  const series = report?.devices.find((entry) => entry.entity_id === entityId);

  return (
    <dialog
      ref={dialog}
      className="m-auto w-[calc(100%-2rem)] max-w-2xl max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-lg border bg-background p-6 text-foreground shadow-lg backdrop:bg-black/50"
      aria-labelledby="measurement-dialog-heading"
      onClose={onClose}
    >
      <div className="grid gap-5">
        <div className="flex items-start justify-between gap-3">
          <h2
            id="measurement-dialog-heading"
            className="min-w-0 text-lg font-semibold leading-snug break-words"
          >
            {name}
          </h2>
          <Button type="button" variant="ghost" size="sm" onClick={() => dialog.current?.close()}>
            <X aria-hidden="true" />
            Close
          </Button>
        </div>
        <fieldset className="flex flex-wrap items-center gap-2">
          <legend className="sr-only">Range</legend>
          {RANGES.map((range) => (
            <Button
              key={range.days}
              type="button"
              size="sm"
              variant={range.days === days ? "default" : "outline"}
              aria-pressed={range.days === days}
              onClick={() => setRequest({ days: range.days, attempt: 0 })}
            >
              {range.label}
            </Button>
          ))}
        </fieldset>
        {shown && "failed" in shown ? (
          <p className="text-sm text-danger" role="status">
            Cannot load measurements.{" "}
            <Button
              type="button"
              variant="link"
              className="h-auto p-0"
              onClick={() => setRequest({ days, attempt: request.attempt + 1 })}
            >
              Retry
            </Button>
          </p>
        ) : !report ? (
          <p className="text-sm text-muted-foreground" role="status">
            Loading…
          </p>
        ) : !series ? (
          <p className="text-sm text-muted-foreground" role="status">
            Home Assistant has no statistics for this device.
          </p>
        ) : (
          <>
            <div className="grid gap-1">
              <Chart range={report} series={series} />
              <p className="flex justify-between text-xs text-muted-foreground">
                <span>{time(report.start)}</span>
                <span>{time(report.end)}</span>
              </p>
            </div>
            {!series.buckets.length && (
              <p className="text-sm text-muted-foreground" role="status">
                Home Assistant has no statistics for this range.
              </p>
            )}
            <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <li className={legend}>
                <span className={cn(swatch, "bg-status-on/30")} aria-hidden="true" />
                Lowest to highest
              </li>
              <li className={legend}>
                <span className={cn(swatch, "h-0.5 bg-status-on-foreground")} aria-hidden="true" />
                Mean
              </li>
              <li className={legend}>
                <span className={cn(swatch, "border bg-muted")} aria-hidden="true" />
                No statistics
              </li>
            </ul>
            <Totals series={series} />
            <p className="text-xs text-muted-foreground">
              {`Each ${series.period === "hour" ? "hour" : "5 minutes"} is one period of Home Assistant's statistics. Periods without statistics are left empty and count toward no number.`}
            </p>
          </>
        )}
      </div>
    </dialog>
  );
}
