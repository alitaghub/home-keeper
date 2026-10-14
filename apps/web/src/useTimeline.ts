import { useCallback, useRef, useState } from "react";
import { api, type Schemas, unwrap } from "./api/client";
import { date } from "./format";

type EventRecord = Schemas["EventRecord"];

export function useTimeline() {
  const busy = useRef(false);
  const cursor = useRef<string | null>(null);
  // Polling stops replacing the list while older pages are open.
  const showingOlder = useRef(false);
  const loaded = useRef<EventRecord[]>([]);
  const [events, setEvents] = useState<EventRecord[] | null>(null);
  const [status, setStatus] = useState("Waiting for recorded history.");
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (older = false) => {
    if (busy.current) return;
    busy.current = true;
    setLoading(true);
    try {
      const history = await unwrap(
        api.GET("/api/events", {
          params: {
            query: older && cursor.current ? { limit: 50, cursor: cursor.current } : { limit: 50 },
          },
        }),
      );
      showingOlder.current = older;
      const previous = older ? loaded.current : [];
      const ids = new Set(previous.map((event) => event.id));
      loaded.current = [...previous, ...history.events.filter((event) => !ids.has(event.id))];
      cursor.current = history.next_cursor;
      setHasMore(Boolean(history.next_cursor));
      setEvents(loaded.current);
      setStatus(
        `${loaded.current.length} changes shown. Retention cutoff: ${date(history.retained_since)}. This cutoff does not establish continuous coverage.${older ? " Older history is open; refresh to see new changes." : " New changes refresh automatically."}`,
      );
    } catch {
      setStatus("Cannot load recorded history. Previously displayed entries may be out of date.");
    } finally {
      busy.current = false;
      setLoading(false);
    }
  }, []);

  return { events, status, hasMore, loading, load, showingOlder };
}
