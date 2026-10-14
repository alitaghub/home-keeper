# Home Assistant and Hue

Home Observer connects to Home Assistant, not directly to the Hue Bridge. Use
the guided setup if you do not run Home Assistant yet. Otherwise skip to
[Connect Home Assistant and Hue](#connect-home-assistant-and-hue).

Container `localhost` is not the computer running Docker. Read
[networking.md](networking.md) before you pick the Home Assistant address.

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

### Restart the configured monitor

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

### Create a token

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

### Start live mode

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

### Discover and select lights

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

### Live data and stopping

Live mode uses its own `live-data` SQLite volume. Keep demo and live data separate.
The API rejects a database containing another source. Stop the demo before
starting live mode because both publish the same localhost port. To stop live
mode while retaining data:

```sh
docker compose -f compose.yaml -f compose.live.yaml down
```

Use both Compose files for live start, restart and stop commands. Adding
`--volumes` to the live stop command deletes its named volumes.

Next, run the [Phase 1 live test checklist](live-test-checklist.md).
