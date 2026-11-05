# Demo

The demo runs a synthetic home. It needs no hardware and no Home Assistant.

## Start

You need Docker Engine with Compose on Linux, or Docker Desktop on WSL2 or
macOS.

```sh
docker compose -f compose.demo.yaml up --build --wait
```

Open <http://localhost:8000>. If port 8000 is taken, set `HOME_KEEPER_PORT`:

```sh
HOME_KEEPER_PORT=18000 docker compose -f compose.demo.yaml up --build --wait
```

Stop the home stack first. Both stacks publish port 8000.

## The synthetic home

| Device               | Entity                     | Kind          | Home tab section   | Behavior                                                     |
| -------------------- | -------------------------- | ------------- | ------------------ | ------------------------------------------------------------ |
| Desk lamp            | `light.hue_desk`           | `light`       | Lights             | On/off, brightness, color temperature, XY color, unavailable |
| Reading lamp         | `light.hue_reading`        | `light`       | Lights             | Same as the desk lamp                                        |
| Smart plug           | `switch.smart_plug`        | `switch`      | Plugs and switches | On, off, unavailable                                         |
| Smart plug · Desk plug power | `sensor.desk_plug_power` | `measurement` | Measurements | 412 W while the plug is on, 0 W while it is off              |
| Ceiling fan          | `fan.ceiling_fan`          | `on_off`      | Other on/off       | "On" or "Off"                                                |
| Front door           | `binary_sensor.front_door` | `on_off`      | Other on/off       | "Open" or "Closed"                                           |
| Garage door          | `cover.garage_door`        | `state`       | Other              | "Open" or "Closed"                                           |
| Living room group    | `light.hue_room`           | —             | Not monitorable    | A group                                                      |
| Ungrouped sensor     | `sensor.ungrouped`         | —             | Not monitorable    | Belongs to no device                                         |

How it behaves:

- Nothing is monitored at start. Click **Monitor** on the **Setup** tab.
- At start, the demo reader creates 24 hours of history. A monitored device
  shows it at once.
- Every 10 seconds the reader plays the next step of a fixed sequence and adds
  it to the history, as Home Assistant's recorder would.
- The power meter has 30 days of synthetic statistics: a daily curve between
  about 100 and 440 W, in 5-minute and hourly buckets.
- History lives in memory. Restarting the reader starts a new synthetic day.

The reading lamp's entity name contains a `<script>` tag on purpose. The
dashboard shows the device name, "Reading lamp", and renders every Home
Assistant name as text. The Playwright suite checks that this name stays inert.

## Switch demo devices

```sh
docker compose -f compose.demo.yaml --profile control up --build --wait
```

A command on a monitored demo light or the plug switches it at once, adds the
change to the history, and holds the device out of the sequence for three
steps. See [control.md](control.md).

## Stop and reset

```sh
docker compose -f compose.demo.yaml restart
docker compose -f compose.demo.yaml down            # keep monitored devices and the command log
docker compose -f compose.demo.yaml down --volumes  # delete demo data
```

The demo runs as Compose project `home-keeper-demo`, with its own volumes. It
never touches the home stack's data.
