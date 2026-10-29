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

/**
 * On, off and unknown time across the report's range. On draws as high as the
 * light's brightness. Unknown time is never drawn as on or off: a gap or an
 * unavailable device is hatched, and time before any record stays blank.
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
      {levels(device).map((period) => {
        const height = period.state === "on" ? period.level * HEIGHT : 2;
        return (
          <rect
            key={period.start}
            className={`strip-${period.state}`}
            x={x(period.start)}
            width={x(period.end) - x(period.start)}
            y={HEIGHT - height}
            height={height}
          />
        );
      })}
      {device.unknown_intervals
        .filter((interval) => interval.reason !== "no_evidence")
        .map((interval) => (
          <rect
            key={interval.start}
            x={x(interval.start)}
            width={x(interval.end) - x(interval.start)}
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
              <span style={{ width: `${(day.on_seconds / 86400) * 100}%` }} />
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

/** One device's recorded usage over a chosen range, in a modal dialog. */
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
  const [days, setDays] = useState<Days>(1);
  // Each answer remembers its range, so switching ranges never shows the old one's numbers.
  const [loaded, setLoaded] = useState<
    { days: Days; report: Usage } | { days: Days; failed: true }
  >();

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  useEffect(() => {
    let current = true;
    fetchUsage(days, [entityId]).then(
      (report) => current && setLoaded({ days, report }),
      () => current && setLoaded({ days, failed: true }),
    );
    return () => {
      current = false;
    };
  }, [days, entityId]);

  const shown = loaded?.days === days ? loaded : undefined;
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
            onClick={() => setDays(range.days)}
          >
            {range.label}
          </button>
        ))}
      </fieldset>
      {shown && "failed" in shown ? (
        <p className="history-status" role="status">
          Cannot load usage. Please retry.
        </p>
      ) : !report ? (
        <p className="history-status" role="status">
          Loading…
        </p>
      ) : !device ? (
        <p className="history-status" role="status">
          No usage recorded for this device.
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
            <li className="legend-unknown">Collection gap or unavailable</li>
            <li className="legend-none">No record yet</li>
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
