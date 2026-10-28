# Synthetic demo

The demo runs without hardware or Home Assistant. Install Docker Engine with
Compose on Linux, or Docker Desktop with Compose on Windows/WSL2 or macOS. From
the repository root:

```sh
docker compose -f compose.demo.yaml up --build --wait
```

Open <http://127.0.0.1:8000>. Set `HOME_KEEPER_PORT` to publish on another loopback
port when 8000 is taken, for example
`HOME_KEEPER_PORT=18000 docker compose -f compose.demo.yaml up --build --wait`.

## What the demo shows

The collector discovers five synthetic entities: two single lights, a room
group, a smart plug, and an ungrouped sensor. The two lights and the plug can be
selected. Click **Monitor** to establish a baseline. Nothing is monitored
automatically. After selection, the demo emits a repeatable sequence of on/off,
brightness, color-temperature, XY color, unavailability and recovery changes,
one step per collection pass. The plug follows the same steps with on, off and
unavailable only, and shows under **Plugs and switches** on the Home tab. The timeline shows only events recorded after
selection. Restarting the collector starts the sequence again; it does not erase
retained history.

Selection takes effect on the next collection pass, normally within 10 seconds.
The old source change date is deliberate. A recent collection can confirm a
state that has not changed for months.

The script-shaped name is deliberate too. The "Reading lamp" device has an
entity name that contains a `<script>` tag. The dashboard labels lamps by device
name, so you see "Reading lamp". Any name that comes from Home Assistant renders
as text, never as HTML, and the Playwright suite checks that this one stays inert.

The dashboard refreshes every three seconds. **Refresh discovery**, on the
**Setup** tab, requests a
new catalog on the next collection pass. A newly discovered light stays
unselected until you choose it.

## Switch a demo light

Add the `control` profile to switch the synthetic lights:

```sh
docker compose -f compose.demo.yaml --profile control up --build --wait
```

A command on a monitored demo light changes it at once, records the change,
and keeps the light out of the replay sequence for three collection passes.
[control.md](control.md) shows how to send one.

## Restart, stop and reset

```sh
docker compose -f compose.demo.yaml restart
# Stop services, keeping selected devices and baselines:
docker compose -f compose.demo.yaml down
```

The demo runs as Compose project `home-keeper-demo`, with its own volumes.
SQLite data and the internal service credential persist there.
`docker compose -f compose.demo.yaml down --volumes` deletes those volumes. It
does not touch the home stack. Stop the home stack before you start the demo;
both publish port 8000. Changing selection never
requires a restart. **Stop monitoring** rejects later observation ingestion.
Previously recorded events stay visible until retention expires.

To change the collection interval or retention, or to test a collection gap with
the demo, see [collection.md](collection.md).
