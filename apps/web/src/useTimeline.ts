import { useCallback, useRef, useState } from "react";
import { api, type CommandRecord, type Schemas, unwrap } from "./api/client";

type ChangeRecord = Schemas["ChangeRecord"];

export function useTimeline() {
  // Each request takes a number and only the latest one's result is applied, so
  // `restart` can discard a page asked for under the previous selection.
  const latest = useRef(0);
  const inFlight = useRef<"newest" | "older" | null>(null);
  const cursor = useRef<string | null>(null);
  // Polling stops replacing the list while older pages are open.
  const showingOlder = useRef(false);
  const loaded = useRef<ChangeRecord[]>([]);
  const [events, setEvents] = useState<ChangeRecord[] | null>(null);
  // Whether the latest read failed, so the list shown may be out of date.
  const [failed, setFailed] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (older = false) => {
    // A newest page in flight answers any request; a second older page would repeat the first.
    if (inFlight.current === "newest" || (older && inFlight.current === "older")) return;
    const seq = ++latest.current;
    const wasOlder = showingOlder.current;
    inFlight.current = older ? "older" : "newest";
    showingOlder.current = older;
    setLoading(true);
    try {
      const history = await unwrap(
        api.GET("/api/events", {
          params: {
            query: older && cursor.current ? { limit: 50, cursor: cursor.current } : { limit: 50 },
          },
        }),
      );
      if (seq !== latest.current) return;
      const previous = older ? loaded.current : [];
      const ids = new Set(previous.map((event) => event.id));
      loaded.current = [...previous, ...history.events.filter((event) => !ids.has(event.id))];
      cursor.current = history.next_cursor;
      setHasMore(Boolean(history.next_cursor));
      setEvents(loaded.current);
      setFailed(false);
    } catch {
      if (seq !== latest.current) return;
      showingOlder.current = wasOlder;
      setFailed(true);
    } finally {
      if (seq === latest.current) {
        inFlight.current = null;
        setLoading(false);
      }
    }
  }, []);

  // After a selection change: drop a removed device's rows at once, discard every
  // request in flight, and let the next load start again from the newest page.
  const restart = useCallback((removed: string | null) => {
    latest.current++;
    inFlight.current = null;
    showingOlder.current = false;
    setLoading(false);
    if (removed === null) return;
    loaded.current = loaded.current.filter((event) => event.entity_id !== removed);
    setEvents((shown) => shown && loaded.current);
  }, []);

  return { events, failed, hasMore, loading, load, restart, showingOlder };
}

// Commands page like events, apart from them: the two lists are not interleaved.
export function useCommands() {
  // The newest page supersedes an older page in flight: "Refresh history" must win
  // over "Load older commands". Only the latest request's result is applied.
  const latest = useRef(0);
  const inFlight = useRef<"newest" | "older" | null>(null);
  const cursor = useRef<string | null>(null);
  // Set when a request starts, so a poll during an older load does not replace it.
  const showingOlder = useRef(false);
  const loaded = useRef<CommandRecord[]>([]);
  const [commands, setCommands] = useState<CommandRecord[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (older = false) => {
    // A newest page in flight answers any request; a second older page would repeat the first.
    if (inFlight.current === "newest" || (older && inFlight.current === "older")) return;
    const seq = ++latest.current;
    const wasOlder = showingOlder.current;
    inFlight.current = older ? "older" : "newest";
    showingOlder.current = older;
    setLoading(true);
    try {
      const page = await unwrap(
        api.GET("/api/commands", {
          params: {
            query: older && cursor.current ? { limit: 20, cursor: cursor.current } : { limit: 20 },
          },
        }),
      );
      if (seq !== latest.current) return;
      const previous = older ? loaded.current : [];
      const ids = new Set(previous.map((command) => command.id));
      loaded.current = [...previous, ...page.commands.filter((command) => !ids.has(command.id))];
      cursor.current = page.next_cursor;
      setHasMore(Boolean(page.next_cursor));
      setCommands(loaded.current);
    } catch {
      // Command history is secondary: a failed read keeps the last list and adds no message.
      if (seq === latest.current) showingOlder.current = wasOlder;
    } finally {
      if (seq === latest.current) {
        inFlight.current = null;
        setLoading(false);
      }
    }
  }, []);

  return { commands, hasMore, loading, load, showingOlder };
}
