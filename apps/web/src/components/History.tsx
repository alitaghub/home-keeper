import { useId } from "react";
import { brightnessPercent, duration, time } from "../format";
import { cn } from "../lib/utils";
import { type DeviceUsage, fetchUsage, type Usage } from "../useUsage";
import { RANGES, RangeDialog } from "./RangeDialog";

const WIDTH = 1000;
const HEIGHT = 24;
// A dim light still shows as a visible bar.
const MIN_LEVEL = 0.2;

/**
 * The words for a device's on and off, capitalized and inline: Home Assistant's words for an
 * on/off device, such as "Open" and "Closed" for a door, else plain on and off.
 */
function wordsOf(device: DeviceUsage) {
  return device.device_kind === "on_off"
    ? { On: device.on_label, Off: device.off_label, on: device.on_label, off: device.off_label }
    : { On: "On", Off: "Off", on: "on", off: "off" };
}

/** How tall each known period draws, from 0 to 1. Off is a thin line; on is its brightness. */
function levels(device: DeviceUsage) {
  if (device.device_kind !== "light") {
    return device.periods.map((period) => ({ ...period, level: period.state === "on" ? 1 : 0 }));
  }
  return device.periods.map(({ brightness, ...period }) => ({
    ...period,
    level:
      period.state === "on" ? (brightness == null ? 1 : Math.max(MIN_LEVEL, brightness / 255)) : 0,
  }));
}

type Span = { x0: number; x1: number };
type Bar = Span & { state: "on" | "off"; level: number };

/**
 * Joins each span narrower than one unit with the spans that follow it closely, so the
 * strip draws at most about WIDTH spans however often the device changed.
 */
function thinned<T extends Span>(spans: T[], join: (first: T, next: T) => T): T[] {
  const kept: T[] = [];
  for (const span of spans) {
    const last = kept.at(-1);
    if (last && last.x1 - last.x0 < 1 && span.x0 - last.x1 < 1) {
      kept[kept.length - 1] = join(last, span);
    } else {
      kept.push(span);
    }
  }
  return kept;
}

/**
 * On, off and unknown time across the report's range. On draws as high as the
 * light's brightness. Unknown time is never drawn as on or off: an unavailable
 * device is hatched, and time Home Assistant has no history for stays blank.
 */
export function UsageStrip({
  range,
  device,
  caption,
}: {
  range: Pick<Usage, "start" | "end">;
  device: DeviceUsage;
  caption: string;
}) {
  // useId may contain characters a url(#…) reference cannot hold.
  const hatch = `hatch-${useId().replace(/[^\w-]/g, "")}`;
  const from = Date.parse(range.start);
  const span = Date.parse(range.end) - from;
  const x = (moment: string) => ((Date.parse(moment) - from) / span) * WIDTH;
  return (
    <svg
      className="block h-6 w-full overflow-hidden rounded-sm"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`${caption}: ${wordsOf(device).on} ${duration(device.on_seconds)}, unknown ${duration(device.unknown_seconds)}`}
    >
      <defs>
        <pattern
          id={hatch}
          width="6"
          height="6"
          patternUnits="userSpaceOnUse"
          patternTransform="rotate(45)"
        >
          <rect className="fill-status-unknown" width="2" height="6" />
        </pattern>
      </defs>
      <rect className="fill-muted" width={WIDTH} height={HEIGHT} />
      {thinned(
        levels(device).map(
          ({ start, end, state, level }): Bar => ({
            x0: x(start),
            x1: x(end),
            state,
            level,
          }),
        ),
        // Within one unit, any on time shows, at its highest level.
        (first, next): Bar => ({
          x0: first.x0,
          x1: next.x1,
          state: first.state === "on" || next.state === "on" ? "on" : "off",
          level: Math.max(first.level, next.level),
        }),
      ).map((bar) => {
        const height = bar.state === "on" ? bar.level * HEIGHT : 2;
        return (
          <rect
            key={bar.x0}
            data-segment={bar.state}
            className={bar.state === "on" ? "fill-status-on" : "fill-muted-foreground"}
            x={bar.x0}
            width={bar.x1 - bar.x0}
            y={HEIGHT - height}
            height={height}
          />
        );
      })}
      {thinned(
        device.unknown_intervals
          .filter((interval) => interval.reason !== "no_history")
          .map((interval) => ({ x0: x(interval.start), x1: x(interval.end) })),
        (first, next) => ({ x0: first.x0, x1: next.x1 }),
      ).map((span) => (
        <rect
          key={span.x0}
          x={span.x0}
          width={span.x1 - span.x0}
          height={HEIGHT}
          data-segment="unavailable"
          fill={`url(#${hatch})`}
        />
      ))}
    </svg>
  );
}

const total = "rounded-md border px-3 py-2";
const totalLabel = "text-xs text-muted-foreground";
const totalValue = "text-lg font-semibold tabular-nums";
const subheading = "mb-2 text-sm font-semibold";

function Totals({ device }: { device: DeviceUsage }) {
  const words = wordsOf(device);
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      <div className={total}>
        <dt className={totalLabel}>{words.On}</dt>
        <dd className={totalValue}>{duration(device.on_seconds)}</dd>
      </div>
      <div className={total}>
        <dt className={totalLabel}>
          {device.device_kind === "on_off" ? `Became ${words.On}` : "Switched on"}
        </dt>
        <dd className={totalValue}>
          {device.switch_ons === 1 ? "1 time" : `${device.switch_ons} times`}
        </dd>
      </div>
      {device.device_kind === "light" && (
        <div className={total}>
          <dt className={totalLabel}>Average brightness</dt>
          <dd className={totalValue}>
            {device.average_brightness == null
              ? "—"
              : `${brightnessPercent(device.average_brightness)}%`}
          </dd>
        </div>
      )}
      <div className={total}>
        <dt className={totalLabel}>Unknown</dt>
        <dd className={totalValue}>{duration(device.unknown_seconds)}</dd>
      </div>
    </dl>
  );
}

function Days({ device }: { device: DeviceUsage }) {
  return (
    <section aria-labelledby="usage-days-heading">
      <h3 id="usage-days-heading" className={subheading}>
        {`${wordsOf(device).On} per day`}
      </h3>
      <ol className="grid gap-1 text-xs">
        {device.days.map((day) => (
          <li key={day.date} className="grid grid-cols-[5.5rem_1fr_4rem] items-center gap-2">
            <span className="text-muted-foreground tabular-nums">{day.date}</span>
            <span className="h-2 overflow-hidden rounded-full bg-muted">
              {/* A day with a daylight-saving change can have 25 hours. */}
              <span
                className="block h-full rounded-full bg-status-on"
                style={{ width: `${Math.min(100, (day.on_seconds / 86400) * 100)}%` }}
              />
            </span>
            <span className="text-right tabular-nums">{duration(day.on_seconds)}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Hours({ device }: { device: DeviceUsage }) {
  const most = Math.max(...device.hours);
  const words = wordsOf(device);
  return (
    <section aria-labelledby="usage-hours-heading">
      <h3 id="usage-hours-heading" className={subheading}>
        {`${words.On} by hour of day`}
      </h3>
      <ol className="flex h-20 items-end gap-px rounded-md bg-muted/50 p-1">
        {device.hours
          .map((seconds, hour) => ({ seconds, hour }))
          .map(({ seconds, hour }) => (
            <li
              key={hour}
              title={`${String(hour).padStart(2, "0")}:00 · ${duration(seconds)}`}
              aria-label={`${String(hour).padStart(2, "0")}:00, ${words.on} ${duration(seconds)}`}
              className="flex h-full flex-1 items-end"
            >
              <span
                className="block w-full rounded-t-sm bg-status-on"
                style={{ height: `${most ? (seconds / most) * 100 : 0}%` }}
              />
            </li>
          ))}
      </ol>
      <p
        className="mt-1 flex justify-between text-xs text-muted-foreground tabular-nums"
        aria-hidden="true"
      >
        <span>00</span>
        <span>06</span>
        <span>12</span>
        <span>18</span>
        <span>24</span>
      </p>
    </section>
  );
}

const legend = "inline-flex items-center gap-1.5";
const swatch = "inline-block h-2.5 w-4 rounded-sm";

/** One device's usage over a chosen range, in a modal dialog. */
export function HistoryDialog({
  entityId,
  name,
  onClose,
}: {
  entityId: string;
  name: string;
  onClose: () => void;
}) {
  return (
    <RangeDialog
      headingId="history-dialog-heading"
      name={name}
      entityId={entityId}
      load={fetchUsage}
      failure="Cannot load usage."
      onClose={onClose}
    >
      {(report, days) => {
        const device = report.devices.find((entry) => entry.entity_id === entityId);
        const label = RANGES.find((range) => range.days === days)?.label ?? "";
        return !device ? (
          <p className="text-sm text-muted-foreground" role="status">
            Home Assistant has no usage for this device.
          </p>
        ) : (
          <>
            <div className="grid gap-1">
              <UsageStrip range={report} device={device} caption={`Last ${label}`} />
              <p className="flex justify-between text-xs text-muted-foreground">
                <span>{time(report.start)}</span>
                <span>{time(report.end)}</span>
              </p>
            </div>
            <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <li className={legend}>
                <span className={cn(swatch, "bg-status-on")} aria-hidden="true" />
                {device.device_kind === "light" ? "On, by brightness" : wordsOf(device).On}
              </li>
              <li className={legend}>
                <span className={cn(swatch, "h-0.5 bg-muted-foreground")} aria-hidden="true" />
                {wordsOf(device).Off}
              </li>
              <li className={legend}>
                <span
                  className={cn(
                    swatch,
                    "bg-[repeating-linear-gradient(45deg,var(--status-unknown)_0_2px,transparent_2px_5px)]",
                  )}
                  aria-hidden="true"
                />
                Unavailable
              </li>
              <li className={legend}>
                <span className={cn(swatch, "border bg-muted")} aria-hidden="true" />
                No history
              </li>
            </ul>
            <Totals device={device} />
            {days > 1 && <Days device={device} />}
            <Hours device={device} />
            <p className="text-xs text-muted-foreground">
              {`Days and hours use the ${report.timezone} time zone. Unknown time counts as neither ${wordsOf(device).on} nor ${wordsOf(device).off}.`}
            </p>
          </>
        );
      }}
    </RangeDialog>
  );
}
