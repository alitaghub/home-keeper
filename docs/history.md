# History and usage

This page explains where Home Keeper's history, usage numbers and measurement
charts come from, and what they can and cannot prove.

## Source

Home Assistant's [recorder](https://www.home-assistant.io/integrations/recorder/)
stores every device's history. Home Keeper reads it on each request and keeps
no copy.

- The reader answers the API's reads: the catalog, current states, history,
  statistics and reachability.
- The reader caches the catalog for up to 10 seconds. It never caches a state,
  history or statistics.
- The dashboard polls every 3 seconds, so the **Home** tab shows what Home
  Assistant reports now.

Home Keeper's own database holds only the monitored devices, the
[command log](control.md#command-history) and a few internal values.

**Monitoring is the only filter.** History, usage and the agent cover only the
devices you monitor now. A device you stop monitoring disappears from History,
including the time you monitored it. Monitoring it again shows all the history
Home Assistant has for it.

How far back history goes is a Home Assistant setting. See
[History retention](home-assistant.md#history-retention).

## Unknown time

Home Keeper never guesses. Time is **unknown** when:

| Reason        | Cause                                                                  |
| ------------- | ---------------------------------------------------------------------- |
| `no_history`  | Before the device's first row in the window, or after now.             |
| `unavailable` | Home Assistant reported the device as `unavailable` or `unknown`.      |

Unknown time counts as neither on nor off. On the dashboard strip, unavailable
time is hatched and time without history is blank.

Home Assistant records what it sees. History has no cause or automation
attribution. Changes Home Assistant missed, for example while it was stopped or
a bridge was offline, are absent, and Home Keeper cannot rebuild them.

## Recorded changes

The **History** tab's **Recorded changes** list shows each change the recorder
stored for the monitored devices.

A **change** is two consecutive history rows that differ in state,
availability, brightness or color. Each change shows at Home Assistant's time,
in plain words:

| Device              | Example                         |
| ------------------- | ------------------------------- |
| Light               | "Desk lamp turned on at 40%"    |
| Other device        | "Front door Closed → Open"      |
| Measurement         | "Desk power 380 → 412 W"        |

**Details** shows the source entity, the stable registry ID, the old and new
values, and the time.

An empty list means Home Assistant recorded no change in the window. It does
not prove that nothing happened.

The list refreshes automatically. **Load older changes** pauses the refresh
and loads the next page; **Refresh history** returns to the newest changes.

### How the reader reads history

The reader calls
[`history/history_during_period`](https://developers.home-assistant.io/docs/api/websocket/)
with `significant_changes_only` off, so brightness and color changes are
included.

- Lights, plugs and switches: one request, with attributes.
- Every other device: without attributes (`no_attributes`), up to 25 devices
  to a request.

All requests of one read share one connection and the
[read limits](home-assistant.md#read-limits).

### `GET /api/events`

| Parameter   | Description                                                      |
| ----------- | ---------------------------------------------------------------- |
| `entity_id` | Optional. Limits the list to one device.                         |
| `start`     | Optional, timezone-aware. Default: 30 days before `end`.         |
| `end`       | Optional, timezone-aware. Default: now.                          |
| `limit`     | 1 to 100. Default: 50.                                           |
| `cursor`    | Opaque. Keeps the first page's window, so pages do not shift.    |

Returns `events`, newest first, and `next_cursor`.

## Usage

Usage covers lights, plugs, switches and other on/off devices. Measurements and
`state` devices have no usage.

For each device, usage reports:

- time on, per local day and per local hour of day,
- the number of switch-ons,
- unknown time, with `unknown_intervals`,
- `periods`: the on and off stretches in time order.

A light also reports its average brightness while on. Each light period carries
`brightness` (0–255; null when Home Assistant reported none, and always null
while off).

An `on_off` device also reports `on_label` and `off_label`: Home Assistant's
words for its on and off, such as "Open" and "Closed" for a door. The dashboard
and the agent use them, so a door sensor reads "Open 3h 12m". A device without
a translation uses `on` and `off`.

### Counting rules

- Each history row counts from its update time until the next row. A device on
  before `start` counts from `start`.
- Only a recorded off-to-on change counts as a switch-on. A switch-on at exactly
  `start` counts in that window, not in the window that ends there.
- A monitored device with no history in the window stays in the report, with
  the whole window unknown.
- `periods` and `unknown_intervals` together cover the whole window without
  overlap. A row that repeats the same state does not start a new period.

### Time zone

Days and hours use the API's time zone, UTC by default. Set an IANA name:

```sh
HOME_KEEPER_TIMEZONE=Europe/Copenhagen docker compose up --build --wait
```

An unknown name stops the API at startup.

### `GET /api/usage`

| Parameter   | Description                                                              |
| ----------- | ------------------------------------------------------------------------ |
| `start`     | Required, timezone-aware.                                                |
| `end`       | Required, timezone-aware.                                                |
| `entity_id` | Optional, repeatable. Without it, every monitored device is included.    |

Each entry in `devices` names its `device_kind`. Unmonitored devices are left
out.

## Measurements

A **measurement** is a sensor with state class `measurement`, such as a power
meter or a thermometer. Its card and charts come from Home Assistant's
[long-term statistics](https://developers.home-assistant.io/docs/core/entity/sensor/#long-term-statistics),
not from history.

On the dashboard, a measurement card shows its current value and unit and a
24-hour sparkline, which reloads once a minute. **History** opens a chart of the
last 24 hours, 7 days or 30 days, with the mean, the min–max band, and the
overall min, mean and max. A period without statistics stays empty.

### How the reader reads statistics

The reader calls `recorder/statistics_during_period` with:

| Field           | Value                                                        |
| --------------- | ------------------------------------------------------------ |
| `statistic_ids` | The measurements' entity IDs, up to 50 to a request          |
| `start_time`, `end_time` | The range                                           |
| `period`        | `5minute` for a range up to 24 hours, else `hour`            |
| `types`         | `mean`, `min`, `max`                                         |

Each row has `start` and `end` in Unix milliseconds and the three values. A row
with a missing or null value fails the read with `invalid_data`.

The answer shape is not yet verified against Home Assistant 2026.10.1.

### `GET /api/measurements`

| Parameter   | Description                                                                   |
| ----------- | ----------------------------------------------------------------------------- |
| `start`     | Required, timezone-aware.                                                     |
| `end`       | Required, timezone-aware. At most 31 days after `start`.                      |
| `entity_id` | Optional, repeatable. Without it, every monitored measurement is included.    |

Each entry in `devices` has:

| Field     | Description                                                                                       |
| --------- | ------------------------------------------------------------------------------------------------- |
| `period`  | `5minute` or `hour`.                                                                              |
| `buckets` | One per period with statistics, oldest first: `start`, `end`, `mean`, `min`, `max`. A missing period is a gap, not a zero. |
| `min`     | The lowest bucket minimum. Null without buckets.                                                  |
| `mean`    | The mean of the bucket means, each period weighted equally. Null without buckets.                 |
| `max`     | The highest bucket maximum. Null without buckets.                                                 |
| `unit`, `latest` | The current reading's unit and value. Null when there is no unit, the state is not a number, or the reading fails. |

If only the current readings fail, the report keeps its buckets and totals,
with `unit` and `latest` null.

## When Home Assistant cannot be reached

- Every tab shows a "Can't reach Home Assistant" banner with a short reason,
  such as a rejected token or no answer.
- Monitored devices stay listed with their state unknown. Switching is off.
- **Setup**, History and usage cannot load. You can still stop monitoring a
  device.
- `GET /api/health` reports `healthy` or `unreachable` with a fixed error code.
  Other routes that need Home Assistant answer `503` with the same code and a
  safe message. No upstream text, address or token reaches the response.

"Healthy" means Home Assistant answered the reader. It does not prove that a
device answered Home Assistant.

To test this without hardware, start the demo, monitor a light, and stop the
reader:

```sh
docker compose -f compose.demo.yaml stop reader   # banner appears, state is unknown
docker compose -f compose.demo.yaml start reader  # banner clears within seconds
```

On the home stack, stop `homeassistant` instead.

## Command log retention

Home Keeper's own retention applies only to the command log. The default is 30
days:

```sh
HOME_KEEPER_RETENTION_DAYS=7 docker compose up --build --wait
```

Retention counts from the time the API sent the command. Cleanup runs at most
once a minute. The command list applies the cutoff at once.
