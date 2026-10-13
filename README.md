# Home Observer

A local smart-home monitor with explicit device discovery and selection. Run a
hardware-free synthetic demo, or connect an existing Home Assistant instance and
record selected individual Hue lights. There is no AI agent or device control yet.

## Start the demo

Install Docker Engine with Compose on Linux, or Docker Desktop with Compose on
Windows/WSL2 or macOS. From this repository:

```sh
docker compose up --build --wait
```

Open <http://127.0.0.1:8000>. The collector discovers five synthetic entities:
two individual Hue lights, a room aggregate, a non-Hue plug, and an ungrouped
sensor. Only individual lights can be selected. Click **Monitor** to establish a
baseline. Nothing is monitored automatically. After selection, the demo emits a
repeatable sequence of on/off, brightness, color-temperature, XY color,
unavailability and recovery changes, one step per collection pass. The timeline
shows only events recorded after selection. Restarting the collector starts the
sequence again; it does not erase retained history.

Selection takes effect on the next collection pass, normally within 10 seconds.
The old source change date is deliberate. A recent collection can confirm a
state that has not changed for months. A script-shaped device name is also
deliberate; the dashboard displays it as text.

The dashboard refreshes every three seconds. **Refresh discovery** requests a
new catalog on the next collection pass. A newly discovered light stays
unselected until you choose it.

```sh
docker compose restart
# Stop services, keeping selected devices and baselines:
docker compose down
```

SQLite data and the internal service credential persist in named volumes.
`docker compose down --volumes` deletes those volumes. Changing selection never
requires a restart. **Stop monitoring** rejects later observation ingestion.
Previously recorded events stay visible until retention expires.

## Guided local setup (optional)

If you do not have Home Assistant, run this from WSL or a Linux terminal:

```sh
./scripts/start-home.sh
```

The script starts a separate Home Assistant Container, waits for its local web
page, and opens a three-stage wizard: account creation, Hue Bridge pairing, then
Home Observer connection. Enter passwords in Home Assistant and paste the token
only into the wizard's hidden terminal input. The wizard stores the token outside
this repo, with mode `600` and owner `10001` for the connector. It may ask for your
local sudo password to set that owner. Public connection settings go to
`~/.local/state/home-observer/local-live.env` (or `$XDG_STATE_HOME/home-observer`).
Nothing is sent to CI or an AI model. Device selection remains explicit.

Home Assistant opens at <http://127.0.0.1:8123>, and the monitor at
<http://127.0.0.1:8000>. This optional container publishes only a loopback port.
It uses a separate persistent configuration volume and network joined only by
the trusted connector, not the Observer API. It has no privileged mode, host
networking, USB or Bluetooth access. WSL/container multicast discovery may fail:
add Philips Hue in Home Assistant using the bridge IP from the Hue app. No router
administration or subnet scan is performed. Container installation has no Home
Assistant apps; see the [official installation guide](https://www.home-assistant.io/installation/linux/).
The upstream guide requires Docker Engine rather than Docker Desktop. Linux/WSL
Engine is the development target for this optional setup; macOS and Docker Desktop
have not been verified. For them, use a separately installed Home Assistant.

To restart the configured monitor without repeating the wizard:

```sh
docker compose --env-file "${XDG_STATE_HOME:-$HOME/.local/state}/home-observer/local-live.env" \
  -f compose.yaml -f compose.live.yaml -f compose.local-live.yaml up --build --wait
```

Stop Home Assistant while keeping its configuration:

```sh
docker compose -p home-observer-ha -f compose.home-assistant.yaml down
```

## Connect Home Assistant and Hue

The default demo/live Compose files do not install Home Assistant. Use the optional
guided setup above, or first run Home Assistant separately
and pair your Hue Bridge through its [Hue integration](https://www.home-assistant.io/integrations/hue/).
Home Observer connects to Home Assistant, rather than directly to the bridge.

Create a dedicated Home Assistant user with permission to read the required
registries and selected entities, then generate a long-lived access token from
that user's profile. Home Assistant tokens do not provide a per-token read-only
entity scope. Treat the token as a powerful credential. The connector restricts
its own operations to fixed discovery, selected-state reads and a read-only
state-change subscription.

Save the token alone in a local file outside this repository. Do not put it in
`.env`, a URL, a shell command argument, the dashboard, or a Compose environment
variable. On Linux/WSL, restrict the file so container user `10001` can read it:

```sh
# Replace this example path with your token file. These commands contain no token.
sudo chown 10001:10001 /absolute/path/to/ha-token
sudo chmod 600 /absolute/path/to/ha-token
```

Docker Desktop file-sharing permissions may differ. Confirm that the mounted
file is readable by container user `10001` without making it publicly readable.
Set the connection origin and absolute token file path, then start live mode:

```sh
export OBSERVER_HA_URL=https://home-assistant.example.test:8123
export OBSERVER_HA_TOKEN_FILE=/absolute/path/to/ha-token
docker compose -f compose.yaml -f compose.live.yaml up --build --wait
```

Use your actual Home Assistant origin, without credentials, a query string or an
API path. The address above is an example. HTTPS certificate verification stays
on. Install a certificate trusted by the container; invalid certificates fail
collection. HTTP is supported for a trusted local network, but does not encrypt
the token in transit.

Open <http://127.0.0.1:8000>. The header identifies Home Assistant mode. Discovery
shows supported lamps by default, using their device names. Room groups, scenes,
sensors and other entities stay in the collapsed **Show other entities** section.
Discovery retains minimal device/entity metadata exposed by Home Assistant. Only enabled
individual Hue V2 lights with recognized registry metadata can be monitored.
Hue groups, other integrations, disabled entities and unknown metadata remain
unsupported. This classification uses the released Home Assistant
[2026.10.1 individual-light contract](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/hue/v2/light.py)
and [group-light contract](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/hue/v2/group.py).
Changed or legacy metadata is rejected rather than guessed.

Select a light to record its first state. Add more devices to Home Assistant and
click **Refresh discovery** to find them without changing configuration or
restarting. Stable entity registry identity keeps selection and history across
an entity name change. A new registry entry reusing an old entity ID does not
inherit approval.

Live mode uses its own `live-data` SQLite volume. Keep demo and live data separate.
The API rejects a database containing another source. Stop the demo before
starting live mode because both publish the same localhost port. To stop live
mode while retaining data:

```sh
docker compose -f compose.yaml -f compose.live.yaml down
```

Use both Compose files for live start, restart and stop commands. Adding
`--volumes` to the live stop command deletes its named volumes.

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

For live mode, add `-f compose.yaml -f compose.live.yaml` to that command. Use a
positive interval in seconds. Freshness becomes stale after twice the interval
or a reported collector failure. Authentication, connection and certificate
errors appear as safe status messages. A failed refresh retains the previous
catalog marked stale. A missing selected entity gets its own collection gap while healthy selected lights
keep their event subscription. It remains selected,
with unknown or stale evidence. This does not prove the bulb is disconnected.

Each accepted snapshot records its stable ID, registry identity, current entity
ID, source, state, availability, receipt time and server-owned persistence time.
Source change, update and report timestamps remain separate. Initial baselines
and polling reconciliation snapshots update the current state, but do not create
change entries. Unchanged polling updates collection confirmation. Freshness
means Home Assistant answered recently, not that the bulb itself answered then.

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
100, default 50. It returns `events`, `next_cursor` and `retained_since`. The
retention cutoff is not proof that collection covered that period continuously.

Retention defaults to 30 days. Set a positive number of days on the API service:

```sh
OBSERVER_RETENTION_DAYS=7 docker compose up --build --wait
```

For live mode, add `-f compose.yaml -f compose.live.yaml`. Retention uses the API
persistence time. Requests trigger cleanup at most once per minute. History reads apply the cutoff
immediately; expired rows can remain on disk until the next cleanup request.
Active selections and their latest known state stay available in a separate
current-state cache. That cache does not establish recent device availability.
Expired current-state caches for removed selections are deleted too. An empty
timeline means no retained changes were recorded, not that nothing changed.

Home Assistant's [Hue integration](https://www.home-assistant.io/integrations/hue/)
receives pushed updates from Hue V2 bridges and polls V1 bridges. Our event stream
reports what Home Assistant observed. There is no Recorder history import,
historical backfill, automation attribution or cause inference. Earlier events
and changes during a connection interruption may be absent. Polling snapshots
can reconcile the current state but cannot reconstruct those missing changes.
Collection gaps and recovery are recorded locally. There is no anomaly detector or
AI investigation yet.


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

To test a gap without hardware, select a demo light and wait for fresh evidence:

```sh
docker compose stop collector
# Leave it stopped for more than 20 seconds at the default interval.
# Read the dashboard: collection should become stale and show a gap.
docker compose start collector
# After the next collection pass, the gap should show recovery.
```

`docker compose restart api` verifies persisted restart gaps. For live mode use
both Compose files with these commands. The synthetic home resets its sequence
when the collector starts; that reset is a recovery snapshot, not inferred
historical device activity.


## Phase 1 live test checklist

Once you are home, use the live setup above and test one individual Hue light:

1. Confirm the bulb appears and responds in Home Assistant first.
2. Open Home Observer, check the **HOME ASSISTANT** label, and select that bulb.
   Wait for fresh evidence and an initial baseline.
3. Change on/off and brightness through the Hue app, leaving each setting for
   about 20 seconds. Confirm attributed entries in **Recorded changes**.
4. Stop the live collector with both Compose files. Wait more than twice the
   configured interval. Confirm stale evidence and an open collection gap.
   Change the bulb through the Hue app while collection is stopped.
5. Start the live collector. Confirm a recovery snapshot and closed gap. The app
   must not invent the transition that happened while it was stopped.
6. Restart the API. Confirm selections and history remain, with a persisted
   restart gap that closes after recovery.

Do not share the token or raw registry payloads when reporting a failure.
Share the safe dashboard error and Home Assistant version. Phase 1 implements
monitoring and evidence collection. Real home hardware validation remains pending.

Baseline history uses `limit` (1–100, default 50) and an integer `cursor` from
`next_cursor`; pages contain newest snapshots first. Event history uses its own
timestamp/ID cursor. Collection passes have a deadline of at least 30 seconds,
scaled by five seconds per selected light plus ten seconds. Buffered changes are
persisted between reads, with healthy heartbeats during long successful passes.
A newer completed HTTP read can correct current state after Home Assistant clock
rollback; events captured during that read retain precedence when newer.

## Architecture

```mermaid
flowchart LR
    H[Hue lights and bridge] --> HA[External Home Assistant]
    HA -->|Registry reads, selected-state reads and state-change events| C[Live collector]
    D[Synthetic home] --> DC[Demo collector]
    C -->|Authenticated typed ingestion| A[FastAPI]
    DC -->|Authenticated typed ingestion| A
    A --> S[(SQLite)]
    B[Local dashboard] -->|Explicit selection and refresh| A
    T[Home Assistant token file] -->|Read only; live collector only| C
    V[Internal credential volume] --> A
    V -->|Read only| C
```

Choose one collector mode. FastAPI is the only SQLite writer. The collector has
no database mount. Discovery describes devices but grants no monitoring approval.
Selection and ingestion use SQLite transactions and a server-owned revision.
Delayed collection cannot bypass a removal or a new selection revision.

## Security boundary

- The Observer API publishes only on `127.0.0.1`. Its API and collector run as an
  unprivileged user, without Linux capabilities, with read-only filesystems
  except their mounted volumes. The optional, separate Home Assistant container
  also publishes a loopback port and runs with its upstream container identity.
- Only the live collector mounts the Home Assistant token file. The API, SQLite
  and browser never receive it. Whole-home registries stay inside the trusted
  connector until it projects minimal metadata. Raw administrative URLs and
  unused registry identifiers do not enter the catalog or database.
- The API creates an internal service credential in a restricted file. The
  collector mounts its volume read-only. Internal endpoints require it.
  Dashboard mutations require an explicit header and validate Host and Origin.
- Device names render as text under a restrictive Content Security Policy.
  Schema validation limits fields and restricts source identity. Error status
  and logs omit submitted secrets and raw connector responses.
- Home Assistant operations are fixed registry reads, approved entity-state
  reads and a state-change subscription filtered inside the connector. There is no arbitrary HTTP passthrough, shell tool, router
  administration endpoint, service call or device-write operation.
- The demo collector uses an internal Docker network. The live collector also
  has an ordinary bridge network to reach Home Assistant. It has network access
  needed by a trusted credential service. No model can use this service as a
  tool. A future agent needs a separate network boundary and approved evidence
  tools before it can be added.

Do not expose the dashboard publicly. It has no user authentication. This version
makes no model calls, so no data leaves the home for a cloud model. A future cloud
investigation mode must document the evidence it sends before enabling it.
Deco and additional vendor adapters require separate review.

## Networking and platform limits

Use an explicit address reachable from the collector container. Container
`localhost` means that container, not the computer running Docker.

- Windows/WSL2: run Compose in the WSL project with Docker Desktop WSL integration
  enabled. Use the dashboard's published localhost address. For Home Assistant
  on the Windows host, Docker Desktop provides `host.docker.internal`; for a
  separate home server, use its reachable LAN address or DNS name. WSL firewall,
  VPN and LAN routing can affect connectivity.
- macOS with Docker Desktop: `host.docker.internal` identifies the host. LAN DNS,
  firewall and VPN rules still apply. Docker Desktop runs containers inside a VM.
- Native Linux Docker Engine: `host.docker.internal` is not supplied automatically.
  Use a reachable LAN address, or configure a host-gateway mapping explicitly.
  Home Assistant must listen on an address reachable from the container network.

These are setup directions, not platform test results. The demo was verified on
WSL2/Linux kernel `6.18.33.2-microsoft-standard-WSL2`, Docker Engine `29.9.0`
`linux/amd64`, and Compose `5.6.0`. Native Windows, macOS and a separate native
Linux deployment have not been tested. Real Hue hardware and a user's Home
Assistant instance have not been tested.

## Development and verification

`mise.toml` pins the development tools (Python 3.13, `uv`, Node.js 24, pnpm) and
defines the tasks. The Python service lives in `apps/observer` inside a uv
workspace; its dependencies are pinned in the root `uv.lock`. The dashboard in
`apps/web` is a Vite + React + TypeScript app in the pnpm workspace; the image
builds it in a Node stage and FastAPI serves the result. The Compose smoke
scripts and the Playwright suite live in `e2e/`.

```sh
mise install
pnpm install
mise run check      # Ruff, Biome, basedpyright, tsc, pytest and Vitest with coverage, script syntax
mise run typecheck  # basedpyright (strict) and tsc
mise run test       # pytest (unit and integration) and Vitest, with coverage
mise run fmt        # Ruff format and Biome format
mise run dev        # Compose demo stack, then the Vite dev server with /api proxied
mise run e2e        # Compose smoke scripts and Playwright, then docker compose down
```

Set `OBSERVER_PORT` to publish the API on another loopback port when 8000 is
taken, for example `OBSERVER_PORT=18000 mise run e2e`.

CI uses hardware-free checks and a Compose demo smoke test. Live connector tests
use synthetic Home Assistant responses, not home credentials. They distinguish
connector behavior from Home Assistant's physical device behavior.

Story 01's local verification recorded 12 passing API tests, Ruff and JavaScript
syntax checks, browser selection/removal and malicious-name rendering, and
Compose startup/restart/smoke checks. One synthetic selection confirmed in 4.6
seconds with ten-second polling. That is not measured Hue latency.

Story 02 passed 41 local tests, including seven independent boundary probes.
Synthetic WebSocket/REST tests verified selected reads, identity changes, failure
recovery, credential sanitization, payload limits and invalid TLS rejection.
An isolated live Compose deployment against a fake Home Assistant passed
startup, selection, collection, outage and recovery checks in 44.31 seconds,
including image build time. This is test duration, not measured Hue latency.
Demo Compose smoke, Ruff, JavaScript syntax and browser DOM checks also passed.
Hosted CI has not run. No Python static typechecker or TypeScript checks are
claimed. Model calls and model cost are zero.

Story 03 passed 55 local tests, including three independent adversarial probes
and a regression check for upgrading existing databases. Demo Compose smoke
passed. Live Compose against a synthetic Home Assistant recorded a subscribed
brightness event and passed outage/recovery checks in 25.842 seconds, including
image build time. Ruff, JavaScript syntax and a headless browser check passed.
The browser displayed attributed events, no injected script nodes and no
horizontal overflow. These checks use synthetic devices. Real Hue validation,
hosted CI and real hardware were not verified in that milestone.

Story 04 completed the Phase 1 collection implementation. Final local verification
passed 73 tests, including three independent coverage probes. The reviewer found
one entity-attribution bug in gap timestamps; it was reproduced and fixed.
Synthetic live Compose passed subscription, outage/recovery and API restart-gap
checks in 28.485 seconds, including image build time. Stopping the demo collector
also produced a heartbeat gap without new device events, then recovered in a
25.402-second check. These durations are test execution times, not Hue latency.
Ruff, JavaScript syntax, demo Compose smoke and browser DOM checks passed.
Real Hue hardware, hosted CI and the other platforms listed above remain untested.

The full review fixes passed 82 local tests. Four major failures were reproduced
before their fixes. Demo Compose and synthetic live Compose passed; the latter
check took 22.789 seconds including startup/build. Ruff, JavaScript syntax, shell
syntax and Compose configuration checks passed. A secret scan found only public
synthetic fixture values. The optional Home Assistant 2026.10.1 container started
on this WSL2/Linux Docker Engine host and returned its onboarding page. Account
creation, real bridge pairing and hardware event capture are still pending.

## License

[MIT](LICENSE). This independent project uses original code, public documentation
and synthetic examples. It contains no employer code or home credentials.
