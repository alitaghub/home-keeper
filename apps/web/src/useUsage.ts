import { useEffect, useState } from "react";
import { api, type Schemas, unwrap } from "./api/client";

export type Usage = Schemas["UsageReport"];
export type DeviceUsage = Usage["devices"][number];

const DAY_MS = 24 * 60 * 60 * 1000;

// Usage changes slowly, so the cards' strips reload once a minute, not on every 3 s poll.
const RECENT_MS = 60_000;
// A light that dims on every poll changes the cards each time; reload for it at most this often.
const MIN_GAP_MS = 10_000;

/** Usage over the `days` before now, for some devices or, without IDs, every monitored one. */
export function fetchUsage(days: number, entityIds?: string[]) {
  const end = new Date();
  const start = new Date(end.getTime() - days * DAY_MS);
  return fetchUsageRange(start.toISOString(), end.toISOString(), entityIds);
}

/** Usage between two ISO 8601 times, for some devices or, without IDs, every monitored one. */
export function fetchUsageRange(start: string, end: string, entityIds?: readonly string[]) {
  return unwrap(
    api.GET("/api/usage", {
      params: { query: { start, end, ...(entityIds && { entity_id: [...entityIds] }) } },
    }),
  );
}

/**
 * Runs `load` one call at a time, starting calls at least `gapMs` apart. Requests that
 * arrive meanwhile collapse into one call afterwards.
 */
function coalesced(load: () => Promise<void>, gapMs: number) {
  let busy = false;
  let queued = false;
  let last = Number.NEGATIVE_INFINITY;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const request = (): void => {
    if (timer !== undefined) return;
    if (busy) {
      queued = true;
      return;
    }
    const wait = last + gapMs - Date.now();
    if (wait > 0) {
      timer = setTimeout(() => {
        timer = undefined;
        request();
      }, wait);
      return;
    }
    busy = true;
    last = Date.now();
    void load().finally(() => {
      busy = false;
      if (queued) {
        queued = false;
        request();
      }
    });
  };
  const cancel = () => {
    clearTimeout(timer);
    timer = undefined;
    queued = false;
  };
  return { request, cancel };
}

/**
 * The last day's usage of every monitored device, reloaded every minute and soon after
 * `stateKey` changes. Null until a load succeeds, and again after a load fails, so a
 * strip never shows an old day as the last 24 hours.
 */
export function useRecentUsage(stateKey: string | null) {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [loader] = useState(() =>
    coalesced(
      () =>
        fetchUsage(1).then(
          (report) => setUsage(report),
          () => setUsage(null),
        ),
      MIN_GAP_MS,
    ),
  );

  useEffect(() => {
    if (stateKey === null) return;
    loader.request();
    const timer = setInterval(loader.request, RECENT_MS);
    return () => {
      clearInterval(timer);
      loader.cancel();
    };
  }, [stateKey, loader]);

  return usage;
}
