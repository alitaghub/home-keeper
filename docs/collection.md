# History and usage

Where Home Keeper's history and usage numbers come from, what they do and do not
prove, and what happens when Home Assistant cannot be reached. Commands use the
home stack (`compose.yaml`). For the demo, add `-f compose.demo.yaml`. For your
own Home Assistant, use `-f compose.yaml -f compose.external.yaml` as described
in [home-assistant.md](home-assistant.md#use-your-own-home-assistant).

## The reader and Home Assistant's recorder

Home Assistant's [recorder](https://www.home-assistant.io/integrations/recorder/)
already stores the history of every device. Home Keeper reads that history
instead of keeping its own copy. The **reader** service holds the Home Assistant
token and answers the API's reads on request: the device list, the state each
device reports now, its history in a time window, a measurement's statistics,
and whether Home Assistant answers. The reader keeps no state. It caches the
device list for up to 10 seconds and never caches a state, history or
statistics.

The API asks the reader each time the dashboard or the agent needs data. The
dashboard polls every 3 seconds, so the **Home** tab shows what Home Assistant
reports now, not a copy that can go stale.

Home Keeper's own database holds only the devices you monitor, the command log
(see [control.md](control.md)) and a few internal values.

## When Home Assistant cannot be reached

When the reader or Home Assistant does not answer, every tab shows a "Can't reach
Home Assistant" banner with a short reason, such as a rejected token or no
answer. Monitored devices stay listed with their state unknown, and the switch
buttons are off. The device list on **Setup**, History and usage cannot load until
Home Assistant answers again. You can still stop monitoring a device.
`GET /api/health` reports `healthy` or `unreachable` with a fixed error code; the
other routes that need Home Assistant answer 503 with the same code and a safe
message, never upstream text, addresses or tokens.

Healthy means Home Assistant answered the reader. It does not prove that a bulb
itself answered Home Assistant.

## Monitoring is the only filter

History, usage and the agent cover only the devices you monitor now. A device
you stop monitoring disappears from History, even for the time you monitored it.
Monitoring it again shows all the history Home Assistant has for it, including
the time it was not monitored.

## Recorded changes

The **Recorded changes** list on the **History** tab shows the changes Home
Assistant's recorder stored for the monitored devices. Each request reads Home
Assistant's history through the reader with
[`history/history_during_period`](https://developers.home-assistant.io/docs/api/websocket/),
with `significant_changes_only` off so brightness and color changes are included.
Lights, plugs and switches are read with their attributes; every other device is
read with `no_attributes`, each in a request of its own on the same connection,
so one busy device cannot fill the answer for the others.
[History retention](home-assistant.md#history-retention) gives the size limit.
A change is two consecutive history rows whose state, availability, brightness or
color differ. Each row says the change in words, such as "Desk lamp turned on at
40%", at Home Assistant's time. A change of any other device, on/off devices
and measurements included, shows Home Assistant's words for the old and new
state, such as "Thermostat Heat → Off" or "Front door Closed → Open"; a
measurement adds its unit, such as "Desk power 380 → 412 W". Its **Details** show the source entity, the stable
registry identity, the old and new values and that time.

The reader keeps only approved light fields. For a plug, switch or other on/off
device, such as a fan or a door sensor, it keeps only the on/off state and
availability, so a plug that reports a brightness never shows
one. For any other device it keeps only the state text, at most 255 characters,
and availability. A measurement also keeps its value, the state as a finite
number or null, and its unit, `unit_of_measurement`, at most 32 characters.
Its history rows are read without attributes, so they carry the unit the
device list read. It drops unrelated attributes, account identifiers and raw logs.
Every reading also carries a `label`, Home Assistant's translated word for the
state; see [State words](home-assistant.md#state-words).

Allowed color fields are `color_mode`, `color_temp_kelvin`, `hs_color`, `xy_color`,
`rgb_color`, `rgbw_color` and `rgbww_color`. Brightness uses integers from 0 to
255. Color temperature uses integer kelvin values from 1000 to 10000. Hue/saturation uses
two finite numbers within 0..360 and 0..100; XY uses two finite numbers within
0..1. RGB, RGBW and RGBWW use three, four or five integer channels within 0..255.
Missing fields stay absent, rather than being inferred from another color format.

History refreshes automatically until you load older entries. **Load older
changes** requests the next page; **Refresh history** returns to the newest
changes. The read-only `GET /api/events` endpoint accepts an optional stable
`entity_id`, timezone-aware `start` and `end` timestamps, a `cursor`, and a `limit`
from 1 to 100, default 50. Without `start`, it covers the 30 days before `end`,
which defaults to now. It returns `events`, newest first, and `next_cursor`. The
cursor is opaque and keeps the first page's window, so later pages do not shift
when new changes arrive. When Home Assistant cannot be read, it answers 503.

An empty list means Home Assistant recorded no change in the window, not that
nothing happened.

## Usage numbers

The read-only `GET /api/usage` endpoint reports, per monitored light, plug,
switch or other on/off device, how long it was on and how often it was switched
on, split by local day and local hour of day. Devices of kind `state`, which
report a state but no on or off, and measurements have no entry. Each entry in `devices` names
its `device_kind`. A light also carries its average brightness while on; a plug
or switch has no brightness field.

An `on_off` device, such as a fan or a door sensor, gets a plug's numbers, with
no brightness, and two more fields: `on_label` and `off_label`, Home Assistant's
words for its on and off, such as "Open" and "Closed" for a door. The dashboard
and the agent use them: a door sensor's card reads "Open 3h 12m", not "on". The
words come from Home Assistant's translations for the device, by its device
class first, then its domain's own, even for a state the range never shows. A
device Home Assistant does not translate has the raw words `on` and `off`. The endpoint needs timezone-aware `start` and `end`
timestamps. Repeat `entity_id` to limit the report to some devices; without it,
the report covers every monitored device. Devices that are not monitored are left
out.

Days and hours use the API's time zone, UTC by default. Set an IANA name on the
API service; an unknown name stops the API at startup:

```sh
HOME_KEEPER_TIMEZONE=Europe/Copenhagen docker compose up --build --wait
```

The numbers come from Home Assistant's history and only count what it shows. If
Home Assistant cannot be read, the endpoint answers 503 with a safe error.

- Each history row counts from the moment Home Assistant updated it until the
  next row. A light on before `start` counts from `start`.
- Only recorded off-to-on changes count as switch-ons. A switch-on at exactly
  `start` counts in that window and not in the window that ends there.
- Time before a device's first row in the window, and time after now, is
  unknown with reason `no_history`. A monitored device with no history in the
  window stays in the report with the whole window unknown.
- Time when Home Assistant reports a device as `unavailable` or `unknown` is
  unknown with reason `unavailable`.
- Unknown time counts as neither on nor off. Each device lists its
  `unknown_intervals`.
- Each device also lists its `periods`: the stretches the history shows it on
  or off, in time order. A light's period carries its `brightness` (0–255, or
  null when Home Assistant did not report one, and always null while off). A
  plug's or switch's period has no brightness. Together, `periods` and
  `unknown_intervals` cover the whole range without overlap. A row that repeats
  the same state does not start a new period.

## Measurements

A measurement is a sensor whose state class is `measurement`, such as a power
meter or a thermometer: Home Assistant keeps a mean, a lowest and a highest
value for it. An energy meter, with the state class `total` or
`total_increasing`, has only a sum. It stays a `state` device, and Home Keeper
does not chart energy totals yet. Its card and chart come
from Home Assistant's
[long-term statistics](https://developers.home-assistant.io/docs/core/entity/sensor/#long-term-statistics),
not from its history. The reader asks for them with
`recorder/statistics_during_period`: one request for every device of the read,
with the mean, the lowest and the highest value of each period.

The read-only `GET /api/measurements` endpoint takes timezone-aware `start` and
`end` timestamps, at most 31 days apart, and an optional repeated `entity_id`.
Without it, the report covers every monitored measurement. Other devices, and
devices that are not monitored, are left out. Each entry in `devices` has:

- `period`: `5minute` for a range up to 24 hours, else `hour`.
- `buckets`: one per period Home Assistant has statistics for, oldest first,
  each with `start`, `end`, `mean`, `min` and `max`. A period without statistics
  has no bucket. It is a gap, not a zero, and the dashboard leaves it empty.
- `min`, the lowest bucket minimum; `mean`, the mean of the bucket means, each
  period weighing the same; and `max`, the highest bucket maximum. All three are
  null when there is no bucket.
- `unit` and `latest`: the unit and value of the reading Home Assistant reports
  now; null when the device has no unit, its state is not a number, such as
  `unavailable`, or it cannot be read.

The dashboard's sparkline uses the last 24 hours and reloads once a minute. If
Home Assistant cannot be read, the endpoint answers 503 with a safe error. The
whole read must finish within 8 seconds, and its answer can be at most 16 MiB.
A measurement's statistics row without a mean, a lowest or a highest value is
data Home Keeper cannot read: the read fails with `invalid_data`.

## Retention

How far back History and usage go is a Home Assistant setting: its recorder
keeps 10 days by default.
[home-assistant.md](home-assistant.md#history-retention) shows how to keep more.
A measurement's charts reach further: Home Assistant keeps its hourly statistics
after it purges the history, and its 5-minute statistics as long as the history.

Home Keeper's own retention applies only to the command log. It defaults to 30
days. Set a positive number of days on the API service:

```sh
HOME_KEEPER_RETENTION_DAYS=7 docker compose up --build --wait
```

For the demo, add `-f compose.demo.yaml`. Retention uses the time the API sent
the command. Requests trigger cleanup at most once per minute; the command list
applies the cutoff at once.

## What Home Assistant can and cannot tell us

Home Assistant's [Hue integration](https://www.home-assistant.io/integrations/hue/)
receives pushed updates from Hue V2 bridges and polls V1 bridges. History shows
what Home Assistant recorded. There is no automation attribution or cause
inference. Changes Home Assistant missed, for example while it was stopped or
while a bridge was offline, are absent, and Home Keeper cannot reconstruct them.

## Upgrading from a release with its own copy

Earlier releases copied device history into Home Keeper's own database. On the
first start of this release, Home Keeper deletes that database file, with its
`-wal` and `-shm` files, and creates a new empty one. The API logs one warning
line that says so. Select your devices again once; History still comes from Home
Assistant. The old command log is gone.

## Test an unreachable Home Assistant without hardware

Start the demo and monitor a light, then stop the reader:

```sh
docker compose -f compose.demo.yaml stop reader
# Every tab shows "Can't reach Home Assistant"; the light's state is unknown.
docker compose -f compose.demo.yaml start reader
# Within a few seconds the banner goes away and the state returns.
```

For the home stack, drop `-f compose.demo.yaml`, or stop `homeassistant` instead
of the reader.
