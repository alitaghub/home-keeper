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

The demo reader serves five synthetic entities: two single lights, a room group,
a smart plug, and an ungrouped sensor. The two lights and the plug can be
selected. Nothing is monitored automatically. Click **Monitor**, and the device's
current state and a day of synthetic history appear at once: the demo reader
generates 24 hours of history when it starts. It then plays a repeatable sequence
of on/off, brightness, color-temperature, XY color and unavailable changes, one
step every 10 seconds, and adds each step to the history, as Home Assistant's
recorder would. The plug follows the same steps with on, off and unavailable
only, and shows under **Plugs and switches** on the Home tab. Restarting the
reader starts a new synthetic day; the demo keeps its history in memory only.

The script-shaped name is deliberate too. The "Reading lamp" device has an
entity name that contains a `<script>` tag. The dashboard labels lamps by device
name, so you see "Reading lamp". Any name that comes from Home Assistant renders
as text, never as HTML, and the Playwright suite checks that this one stays inert.

The dashboard refreshes every three seconds and reads each device's state from the
demo reader every time. A newly listed light stays unselected until you choose it.

## Switch a demo light

Add the `control` profile to switch the synthetic lights and plug:

```sh
docker compose -f compose.demo.yaml --profile control up --build --wait
```

A command on a monitored demo light or the plug changes it at once, adds the
change to the demo history, and keeps the device out of the replay sequence for
three steps.
[control.md](control.md) shows how to send one.

## Restart, stop and reset

```sh
docker compose -f compose.demo.yaml restart
# Stop services, keeping the monitored devices and the command log:
docker compose -f compose.demo.yaml down
```

The demo runs as Compose project `home-keeper-demo`, with its own volumes.
Postgres data and the internal service credential persist there.
`docker compose -f compose.demo.yaml down --volumes` deletes those volumes. It
does not touch the home stack. Stop the home stack before you start the demo;
both publish port 8000. Changing selection never
requires a restart. **Stop monitoring** removes the device from History at once.

To change the command retention, or to test an unreachable Home Assistant with
the demo, see [collection.md](collection.md).
