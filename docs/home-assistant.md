# Home Assistant

Home Observer reads devices through Home Assistant. It never talks to device
vendors or bridges directly. Home Assistant owns every device connection.

By default, `docker compose up` starts a bundled Home Assistant and sets it up
for you. If you already run Home Assistant, see
[Use your own Home Assistant](#use-your-own-home-assistant).

## Bundled Home Assistant

From the repository root:

```sh
docker compose up --build --wait
```

This starts four services:

- `homeassistant`: Home Assistant `2026.10.1`, published on
  <http://127.0.0.1:8123> (set `HA_PORT` to change the port).
- `ha-setup`: a one-shot container that sets up Home Assistant, then exits.
- `api`: the Observer API and dashboard, published on <http://127.0.0.1:8000>
  (set `OBSERVER_PORT` to change the port).
- `collector`: the live collector that reads Home Assistant.

### What setup does

`ha-setup` completes Home Assistant onboarding through its HTTP API, with no
clicks:

1. It creates the owner user `owner` with a random password.
2. It finishes onboarding. Analytics stays disabled. Location, time zone and
   units keep Home Assistant's defaults; change them in Home Assistant.
3. It creates a long-lived token for the collector and stores it in the
   `ha-token` volume with mode `600`. Only the collector mounts that volume,
   read-only.
4. It stores the owner login with mode `600` in the `ha-owner` volume. Only
   `ha-setup` mounts that volume.

Setup logs the password once. Print the login again at any time:

```sh
docker compose run --rm --no-deps ha-setup login
```

You can change the password in Home Assistant under your profile. The stored
copy then goes stale, but setup does not need it again.

`ha-setup` runs on every `up`:

- A valid token is stored: it does nothing.
- Home Assistant is not onboarded yet: it onboards it.
- Home Assistant is onboarded but no token is stored, or Home Assistant rejects
  the token (for example, you revoked it): `up` fails. Reset with
  `docker compose down --volumes`, which also deletes Observer history, or use
  [your own Home Assistant](#use-your-own-home-assistant) with a token you create.

### Connect your devices

1. Print the login with the command above.
2. Open <http://127.0.0.1:8123> and log in as `owner`.
3. Go to **Settings → Devices & services → Add integration** and add your
   devices. For Philips Hue, use the [Hue integration](https://www.home-assistant.io/integrations/hue/)
   and enter the bridge IP address from the Hue app.
4. Open <http://127.0.0.1:8000> and click **Refresh discovery**.

Setup is not specific to Hue. You can add any integration Home Assistant
supports. Home Observer records only individual Hue lights today; other
entities appear under **Show other entities**. When no supported device exists
yet, the dashboard links to the Home Assistant integrations page.

### Limits

- Home Assistant runs on a Docker bridge network, not host networking.
  Multicast and mDNS discovery may not see your LAN. If a device is not
  offered, add its integration by IP address.
- USB and Bluetooth integrations are not supported in this setup.
- Home Assistant runs with its upstream container identity, without privileged
  mode. It publishes only a loopback port.
- Container installs have no Home Assistant apps. See the
  [official installation guide](https://www.home-assistant.io/installation/linux/).
- Linux and WSL2 with Docker Engine are tested, including pairing a real Hue
  Bridge. macOS with Docker Desktop should work but has not been tested.

### Stop and reset

```sh
# Stop, keeping Home Assistant, its token and Observer history:
docker compose down
# Delete everything, including your Home Assistant setup:
docker compose down --volumes
```

## Use your own Home Assistant

`compose.external.yaml` replaces the bundled Home Assistant and `ha-setup` with
a Home Assistant you already run. Container `localhost` is not the computer
running Docker. Read [networking.md](networking.md) before you pick the address.

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

### Start

Set the connection origin and absolute token file path, then start with both
Compose files:

```sh
export OBSERVER_HA_URL=https://home-assistant.example.test:8123
export OBSERVER_HA_TOKEN_FILE=/absolute/path/to/ha-token
# Optional: the address your browser uses, for the dashboard's link to Home Assistant.
export OBSERVER_HA_PUBLIC_URL=https://home-assistant.example.test:8123
docker compose -f compose.yaml -f compose.external.yaml up --build --wait
```

Use your actual Home Assistant origin, without credentials, a query string or an
API path. The address above is an example. HTTPS certificate verification stays
on. Install a certificate trusted by the container; invalid certificates fail
collection. HTTP is supported for a trusted local network, but does not encrypt
the token in transit.

Use both Compose files for every command, for example
`docker compose -f compose.yaml -f compose.external.yaml down`.

## Discover and select lights

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

## Home and demo data

The home stack and the demo use separate Compose projects and volumes. The demo
runs as project `home-observer-demo`. The API rejects a database containing
another source. Stop one stack before starting the other, because both publish
the same localhost port.

## Check it with a real light

Test one individual Hue light end to end:

1. Confirm the bulb appears and responds in Home Assistant.
2. Open Home Observer, check the **HOME ASSISTANT** label, and select the bulb.
   Wait for fresh evidence and a first baseline.
3. Change on/off and brightness in the Hue app. Leave each setting for about 20
   seconds. Confirm the changes appear in **Recorded changes**.
4. Stop the live collector and wait more than twice the collection interval.
   Confirm the evidence turns stale and a collection gap opens. Change the bulb
   while collection is stopped.
5. Start the collector again. Confirm a recovery snapshot and a closed gap. The
   app must not invent the change that happened while it was stopped.
6. Restart the API. Confirm selections and history remain, and that a restart
   gap opens and then closes.

[collection.md](collection.md#test-a-gap-without-hardware) has the commands for
steps 4 to 6. For the home stack, drop `-f compose.demo.yaml` from each.

When you report a failure, share the dashboard's error message and your Home
Assistant version. Never share the token or raw registry payloads.
