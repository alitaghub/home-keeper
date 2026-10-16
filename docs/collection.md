# Collection, evidence and gaps

What the collector records, what that evidence does and does not prove, and how
interruptions show up. Commands use the home stack (`compose.yaml`). For the
demo, add `-f compose.demo.yaml`. For your own Home Assistant, use
`-f compose.yaml -f compose.external.yaml` as described in
[home-assistant.md](home-assistant.md#use-your-own-home-assistant).

## Collection and evidence limits

The collector refreshes discovery and polls selected current states in each
collection pass. Live mode also subscribes to Home Assistant state-change events
while at least one light is selected. The trusted connector discards unselected
events before forwarding them to the API. An empty selection opens no event
subscription. It waits ten seconds between passes by default. Slow requests
can lengthen the interval. Change the interval without storing a secret:

```sh
OBSERVER_RECONCILIATION_SECONDS=30 docker compose up --build --wait
```

For the demo, run `OBSERVER_RECONCILIATION_SECONDS=30 docker compose -f compose.demo.yaml up --build --wait`.
Use a positive interval in seconds. Freshness becomes stale after twice the interval
or a reported collector failure. Authentication, connection and certificate
errors appear as safe status messages. A failed refresh retains the previous
catalog marked stale. A missing selected entity gets its own collection gap while healthy selected lights
keep their event subscription. It remains selected,
with unknown or stale evidence. This does not prove the bulb is disconnected.

Collection passes have a deadline of at least 30 seconds, scaled by five seconds
per selected light plus ten seconds. Buffered changes are persisted between
reads, with healthy heartbeats during long successful passes. A newer completed
HTTP read can correct current state after Home Assistant clock rollback; events
captured during that read retain precedence when newer.

### Snapshots

Each accepted snapshot records its stable ID, registry identity, current entity
ID, source, state, availability, receipt time and server-owned persistence time.
Source change, update and report timestamps remain separate. Initial baselines
and polling reconciliation snapshots update the current state, but do not create
change entries. Unchanged polling updates collection confirmation. Freshness
means Home Assistant answered recently, not that the bulb itself answered then.

Baseline history uses `limit` (1–100, default 50) and an integer `cursor` from
`next_cursor`; pages contain newest snapshots first.

### Recorded changes

The **Recorded changes** timeline stores observed state, availability, brightness
and color transitions. Each entry includes its evidence ID, source entity and
stable registry identity, old and new values, Home Assistant's `time_fired`,
connector receipt time and API persistence time. Recorded changes use the
[Home Assistant WebSocket `state_changed` subscription](https://developers.home-assistant.io/docs/api/websocket/#subscribe-to-events).
The connector keeps only approved light fields. It drops unrelated attributes,
account identifiers and raw logs. Duplicate delivery creates one event. A late
event remains in history without replacing a newer current state.

Allowed color fields are `color_mode`, `color_temp_kelvin`, `hs_color`, `xy_color`,
`rgb_color`, `rgbw_color` and `rgbww_color`. Brightness uses integers from 0 to
255. Color temperature uses integer kelvin values from 1000 to 10000. Hue/saturation uses
two finite numbers within 0..360 and 0..100; XY uses two finite numbers within
0..1. RGB, RGBW and RGBWW use three, four or five integer channels within 0..255.
Missing fields stay absent, rather than being inferred from another color format.

History refreshes automatically until you load older entries. **Load older
changes** requests another bounded page; **Refresh history** returns to the newest
records. Events from removed selections remain readable until retention expires.
The read-only `GET /api/events` endpoint accepts optional `source_id`, stable
`entity_id`, timezone-aware `start` and `end` timestamps, a `cursor`, and a `limit` from 1 to
100, default 50. It returns `events`, `next_cursor` and `retained_since`. Event
history uses its own timestamp/ID cursor, separate from baseline history. The
retention cutoff is not proof that collection covered that period continuously.

### Retention

Retention defaults to 30 days. Set a positive number of days on the API service:

```sh
OBSERVER_RETENTION_DAYS=7 docker compose up --build --wait
```

For the demo, add `-f compose.demo.yaml`. Retention uses the API
persistence time. Requests trigger cleanup at most once per minute. History reads apply the cutoff
immediately; expired rows can remain on disk until the next cleanup request.
Active selections and their latest known state stay available in a separate
current-state cache. That cache does not establish recent device availability.
Expired current-state caches for removed selections are deleted too. An empty
timeline means no retained changes were recorded, not that nothing changed.

### Usage numbers

The read-only `GET /api/usage` endpoint reports, per selected light, how long it
was on, how often it was switched on and its average brightness while on, split
by local day and local hour of day. It needs timezone-aware `start` and `end`
timestamps. Repeat `entity_id` to limit the report to some lights; without it,
the report covers every selected light. Unselected IDs are left out.

Days and hours use the API's time zone, UTC by default. Set an IANA name on the
API service; an unknown name stops the API at startup:

```sh
OBSERVER_TIMEZONE=Europe/Copenhagen docker compose up --build --wait
```

The numbers only count what the evidence shows:

- A snapshot counts from the moment it was read, and a change from the moment
  Home Assistant fired it. A later snapshot never rewrites an earlier stretch.

  If Home Assistant's clock runs ahead of the observer's, a snapshot can say a
  light last changed after the moment it was read. Usage still counts that
  snapshot from when it was read, but the current state keeps it and ignores
  changes stamped earlier. Until delayed changes arrive, the usage report and
  the current state can disagree about that light. They agree again once the
  change matching the snapshot, a newer change, or a later snapshot is
  recorded. If that change was lost, they disagree until the light changes
  again or a new snapshot is read. Keep both clocks in sync with NTP to avoid
  this.
- A light on before `start` counts from `start`. Only recorded off-to-on
  changes count as switch-ons.
- Collection gaps count as neither on nor off. After recovery, time stays
  unknown, as part of the gap, until a new snapshot or change shows the state.
- Time when Home Assistant reports a light as `unavailable` or `unknown` is
  unknown too, never on or off.
- Time before a light's first evidence, or before the retention cutoff, is
  unknown.
- Each light lists its `unknown_intervals`, with reason `gap`, `unavailable` or
  `no_evidence`.

Selection history is not recorded. If you remove a light and select it again
later, evidence from before the removal can fill the time it was not selected.

### What Home Assistant can and cannot tell us

Home Assistant's [Hue integration](https://www.home-assistant.io/integrations/hue/)
receives pushed updates from Hue V2 bridges and polls V1 bridges. Our event stream
reports what Home Assistant observed. There is no Recorder history import,
historical backfill, automation attribution or cause inference. Earlier events
and changes during a connection interruption may be absent. Polling snapshots
can reconcile the current state but cannot reconstruct those missing changes.
Collection gaps and recovery are recorded locally.

## Collection gaps and recovery

The **Collection gaps** panel records interruptions separately from a bulb
reporting `unavailable`. Losing the connector does not create unavailable events
for every light. Each gap has an evidence ID, selected entity scope, last
confirmed collection, detection time and eventual recovery time. The exact start
is uncertain. A restart records a gap from the last persisted collection point.
A missed heartbeat is detected when the API is read after twice the configured
collection interval. If the API cannot accept a failure record, missing heartbeats
or the next application startup still expose interrupted collection.

On reconnect, the live collector refreshes the registry and current selection,
installs the selected event subscription, and reads current snapshots. A bounded
reader holds events arriving during those reads. It saves snapshots and events
before reporting recovery. Removing or adding a selection invalidates an older
collector revision, including a delayed recovery signal. Renamed identities keep
history; a replacement entity never inherits permission.

Recovery snapshots link to gap evidence and mark transition time as unknown.
For example, if the saved state was off and the recovered state is on, the app
shows the new snapshot without inventing an off-to-on event during the gap.
A normal polling snapshot that contradicts recorded state also marks incomplete
collection. Genuine Home Assistant events remain separate timeline entries.

The recovery buffer is limited to 256 projected events and 1 MiB. Overflow,
invalid data or failed ingestion interrupts collection and requires a new
subscription and fresh snapshots. Transient retries use exponential delays
capped at 60 seconds. Authentication or configuration failures stop the live
collector. Fix the local configuration and restart it. A successful connection
or heartbeat alone does not close a gap.

The read-only `GET /api/coverage?limit=20` endpoint returns at most 100 gaps,
current collection status, last confirmation and retention cutoff. Closed gaps
expire after the retention period measured from recovery. Active gaps remain until recovery. History outside
recorded collection windows remains unknown; no view claims uninterrupted history.

### Test a gap without hardware

Start the demo, select a light and wait for fresh evidence:

```sh
docker compose -f compose.demo.yaml stop collector
# Leave it stopped for more than 20 seconds at the default interval.
# Read the dashboard: collection should become stale and show a gap.
docker compose -f compose.demo.yaml start collector
# After the next collection pass, the gap should show recovery.
```

`docker compose -f compose.demo.yaml restart api` verifies persisted restart
gaps. For the home stack, drop `-f compose.demo.yaml`. The synthetic home resets its sequence
when the collector starts; that reset is a recovery snapshot, not inferred
historical device activity.
