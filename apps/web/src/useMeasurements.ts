import { useEffect, useState } from "react";
import { api, type Schemas, unwrap } from "./api/client";
import { coalesced } from "./useUsage";

export type Measurements = Schemas["MeasurementReport"];
export type Series = Measurements["devices"][number];

const DAY_MS = 24 * 60 * 60 * 1000;

// The cards' sparklines use 5-minute statistics, so they reload once a minute.
const RECENT_MS = 60_000;
const MIN_GAP_MS = 10_000;

/** Statistics over the `days` before now, for some devices or, without IDs, every monitored
 * measurement. */
export function fetchMeasurements(days: number, entityIds?: string[]) {
  const end = new Date();
  const start = new Date(end.getTime() - days * DAY_MS);
  return unwrap(
    api.GET("/api/measurements", {
      params: {
        query: {
          start: start.toISOString(),
          end: end.toISOString(),
          ...(entityIds && { entity_id: entityIds }),
        },
      },
    }),
  );
}

/**
 * The last day's statistics of every monitored measurement, reloaded every minute and soon
 * after `key` changes. A null key, such as when no measurement is monitored, loads nothing.
 * Null until a load succeeds, and again after a load fails.
 */
export function useRecentMeasurements(key: string | null) {
  const [measurements, setMeasurements] = useState<Measurements | null>(null);
  const [loader] = useState(() =>
    coalesced(
      () =>
        fetchMeasurements(1).then(
          (report) => setMeasurements(report),
          () => setMeasurements(null),
        ),
      MIN_GAP_MS,
    ),
  );

  useEffect(() => {
    if (key === null) return;
    loader.request();
    const timer = setInterval(loader.request, RECENT_MS);
    return () => {
      clearInterval(timer);
      loader.cancel();
    };
  }, [key, loader]);

  return measurements;
}
