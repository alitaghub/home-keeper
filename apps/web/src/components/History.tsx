import { useEffect, useId, useRef, useState } from "react";
import { brightnessPercent, duration, time } from "../format";
import { type DeviceUsage, fetchUsage, type Usage } from "../useUsage";

const WIDTH = 1000;
const HEIGHT = 24;
// A dim light still shows as a visible bar.
const MIN_LEVEL = 0.2;

const RANGES = [
  { days: 1, label: "24 hours" },
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
] as const;
type Days = (typeof RANGES)[number]["days"];

/** How tall each known period draws, from 0 to 1. Off is a thin line; on is its brightness. */
function levels(device: DeviceUsage) {
  if (device.device_kind === "switch") {
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
      className="strip"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`${caption}: on ${duration(device.on_seconds)}, unknown ${duration(device.unknown_seconds)}`}
    >
      <defs>
        <pattern
          id={hatch}
          width="6"
          height="6"
          patternUnits="userSpaceOnUse"
          patternTransform="rotate(45)"
        >
          <rect className="strip-hatch" width="2" height="6" />
        </pattern>
      </defs>
      <rect className="strip-track" width={WIDTH} height={HEIGHT} />
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
            className={`strip-${bar.state}`}
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
          fill={`url(#${hatch})`}
        />
      ))}
    </svg>
  );
}

function Totals({ device }: { device: DeviceUsage }) {
  return (
    <dl className="totals">
      <div>
        <dt>On</dt>
        <dd>{duration(device.on_seconds)}</dd>
      </div>
      <div>
        <dt>Switched on</dt>
        <dd>{device.switch_ons === 1 ? "1 time" : `${device.switch_ons} times`}</dd>
      </div>
      {device.device_kind === "light" && (
        <div>
          <dt>Average brightness</dt>
          <dd>
            {device.average_brightness == null
              ? "—"
              : `${brightnessPercent(device.average_brightness)}%`}
          </dd>
        </div>
      )}
      <div>
        <dt>Unknown</dt>
        <dd>{duration(device.unknown_seconds)}</dd>
      </div>
    </dl>
  );
}

function Days({ device }: { device: DeviceUsage }) {
  return (
    <section aria-labelledby="usage-days-heading">
      <h3 id="usage-days-heading">On per day</h3>
      <ol className="day-bars">
        {device.days.map((day) => (
          <li key={day.date}>
            <span>{day.date}</span>
            <span className="bar">
              {/* A day with a daylight-saving change can have 25 hours. */}
              <span style={{ width: `${Math.min(100, (day.on_seconds / 86400) * 100)}%` }} />
            </span>
            <span>{duration(day.on_seconds)}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Hours({ device }: { device: DeviceUsage }) {
  const most = Math.max(...device.hours);
  return (
    <section aria-labelledby="usage-hours-heading">
      <h3 id="usage-hours-heading">On by hour of day</h3>
      <ol className="hour-bars">
        {device.hours
          .map((seconds, hour) => ({ seconds, hour }))
          .map(({ seconds, hour }) => (
            <li
              key={hour}
              title={`${String(hour).padStart(2, "0")}:00 · ${duration(seconds)}`}
              aria-label={`${String(hour).padStart(2, "0")}:00, on ${duration(seconds)}`}
            >
              <span style={{ height: `${most ? (seconds / most) * 100 : 0}%` }} />
            </li>
          ))}
      </ol>
      <p className="hour-axis" aria-hidden="true">
        <span>00</span>
        <span>06</span>
        <span>12</span>
        <span>18</span>
        <span>24</span>
      </p>
    </section>
  );
}

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
  const dialog = useRef<HTMLDialogElement>(null);
  // A retry asks again for the same range.
  const [request, setRequest] = useState({ days: 1 as Days, attempt: 0 });
  const { days } = request;
  // Each answer remembers its request, so a range switch or a retry never shows old numbers.
  const [loaded, setLoaded] = useState<
    { request: typeof request; report: Usage } | { request: typeof request; failed: true }
  >();

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  useEffect(() => {
    let current = true;
    fetchUsage(request.days, [entityId]).then(
      (report) => current && setLoaded({ request, report }),
      () => current && setLoaded({ request, failed: true }),
    );
    return () => {
      current = false;
    };
  }, [request, entityId]);

  const shown = loaded?.request === request ? loaded : undefined;
  const report = shown && "report" in shown ? shown.report : undefined;
  const device = report?.devices.find((entry) => entry.entity_id === entityId);
  const label = RANGES.find((range) => range.days === days)?.label ?? "";

  return (
    <dialog
      ref={dialog}
      className="history-dialog"
      aria-labelledby="history-dialog-heading"
      onClose={onClose}
    >
      <div className="section-heading">
        <h2 id="history-dialog-heading">{name}</h2>
        <button type="button" className="quiet" onClick={() => dialog.current?.close()}>
          Close
        </button>
      </div>
      <fieldset className="ranges">
        <legend>Range</legend>
        {RANGES.map((range) => (
          <button
            key={range.days}
            type="button"
            aria-pressed={range.days === days}
            onClick={() => setRequest({ days: range.days, attempt: 0 })}
          >
            {range.label}
          </button>
        ))}
      </fieldset>
      {shown && "failed" in shown ? (
        <p className="history-status" role="status">
          Cannot load usage.{" "}
          <button
            type="button"
            className="quiet"
            onClick={() => setRequest({ days, attempt: request.attempt + 1 })}
          >
            Retry
          </button>
        </p>
      ) : !report ? (
        <p className="history-status" role="status">
          Loading…
        </p>
      ) : !device ? (
        <p className="history-status" role="status">
          Home Assistant has no usage for this device.
        </p>
      ) : (
        <>
          <UsageStrip range={report} device={device} caption={`Last ${label}`} />
          <p className="strip-axis">
            <span>{time(report.start)}</span>
            <span>{time(report.end)}</span>
          </p>
          <ul className="legend">
            <li className="legend-on">
              {device.device_kind === "light" ? "On, by brightness" : "On"}
            </li>
            <li className="legend-off">Off</li>
            <li className="legend-unknown">Unavailable</li>
            <li className="legend-none">No history</li>
          </ul>
          <Totals device={device} />
          {days > 1 && <Days device={device} />}
          <Hours device={device} />
          <p className="history-status">
            {`Days and hours use the ${report.timezone} time zone. Unknown time counts as neither on nor off.`}
          </p>
        </>
      )}
    </dialog>
  );
}
