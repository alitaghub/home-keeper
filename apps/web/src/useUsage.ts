import { useEffect, useRef, useState } from "react";
import { api, type Schemas, unwrap } from "./api/client";

export type Usage = Schemas["UsageReport"];
export type DeviceUsage = Usage["devices"][number];

const DAY_MS = 24 * 60 * 60 * 1000;

// Usage changes slowly, so the cards' strips reload once a minute, not on every 3 s poll.
const RECENT_MS = 60_000;

/** Usage over the `days` before now, for some devices or, without IDs, every monitored one. */
export function fetchUsage(days: number, entityIds?: string[]) {
  const end = new Date();
  const start = new Date(end.getTime() - days * DAY_MS);
  return unwrap(
    api.GET("/api/usage", {
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
 * The last day's usage of every monitored device. Reloads every minute and whenever
 * `stateKey` changes, so a switched light's strip follows without waiting a minute.
 * Null until the first load succeeds.
 */
export function useRecentUsage(stateKey: string | null) {
  const [usage, setUsage] = useState<Usage | null>(null);
  // A slow answer must not replace a newer one.
  const order = useRef({ started: 0, applied: 0 });

  useEffect(() => {
    if (stateKey === null) return;
    const load = async () => {
      const seq = ++order.current.started;
      try {
        const report = await fetchUsage(1);
        if (seq < order.current.applied) return;
        order.current.applied = seq;
        setUsage(report);
      } catch {
        // Keep the last strips. The main poll already says when Home Keeper is unreachable.
      }
    };
    void load();
    const timer = setInterval(() => void load(), RECENT_MS);
    return () => clearInterval(timer);
  }, [stateKey]);

  return usage;
}
