# Home Assistant

Home Keeper reads devices through Home Assistant. It never talks to device
vendors or bridges directly. Home Assistant owns every device connection.

By default, `docker compose up` starts a bundled Home Assistant and sets it up
for you. If you already run Home Assistant, see
[Use your own Home Assistant](#use-your-own-home-assistant).

## Bundled Home Assistant

From the repository root:

```sh
docker compose up --build --wait
```

This starts five services:

- `homeassistant`: Home Assistant `2026.10.1`, published on
  <http://127.0.0.1:8123> (set `HA_PORT` to change the port).
- `ha-setup`: a one-shot container that sets up Home Assistant, then exits.
- `web`: the dashboard, published on <http://127.0.0.1:8000> (set
  `HOME_KEEPER_PORT` to change the port). It forwards `/api` to the API.
- `api`: the Home Keeper API. It publishes no port.
- `reader`: reads devices, their states and their history from Home Assistant
  for the API. It publishes no port.

With `--profile control`, a sixth service, `controller`, switches monitored
lights, plugs and switches. See [Switch lights](#switch-lights).

### What setup does

`ha-setup` completes Home Assistant onboarding through its HTTP API, with no
clicks:

1. It creates the owner user `owner` with a random password.
2. It finishes onboarding. Analytics stays disabled. Location, time zone and
   units keep Home Assistant's defaults; change them in Home Assistant.
3. It creates a long-lived token for the reader and stores it in the
   `ha-token` volume with mode `600`. Besides `ha-setup`, which writes and
   checks it, only the reader mounts that volume, read-only.
4. It stores the owner login with mode `600` in the `ha-owner` volume. Only
   `ha-setup` mounts that volume.
5. With the reader's owner token, it creates the user "Home Keeper control"
   (username `home-keeper-control`) in Home Assistant's non-admin **Users**
   group, with a random password it does not keep. It creates a long-lived token for that user and stores it in
   the `ha-control-token` volume with mode `600`. Only `ha-setup` and the
   controller mount that volume, and the controller mounts it read-only.

Setup creates the control user even when you do not use the `control` profile,
so turning control on later needs no extra step.

Print the login at any time. Setup never writes the password to its log:

```sh
docker compose run --rm --no-deps ha-setup login
```

You can change the password in Home Assistant under your profile. The stored
copy then goes stale, but setup does not need it again.

`ha-setup` runs on every `up`:

- Both tokens are stored and valid: it keeps the existing setup.
- The reader's token is stored but the control token is not, as in an
  install from before light control: it creates the control user and its token
  with the reader's token. It does not need the owner password. A control
  user left without a stored token, for example by an interrupted run, is
  replaced. If Home Assistant refuses, for example because another account uses
  the username `home-keeper-control`, setup still succeeds and the reader
  starts. Setup logs that light control is off and tries again on the next `up`.
- Home Assistant rejects the stored control token (for example, you revoked
  it): setup still succeeds and the reader starts. Setup logs that light control
  is off and changes nothing. To turn control on again, delete the control token
  file and run `up`:
  `docker compose run --rm --no-deps --entrypoint rm ha-setup /ha-control-token/token`.
  The running controller reads the new token, so control turns on within 30
  seconds without a restart.
- Home Assistant is not onboarded yet: it onboards it.
- Home Assistant is onboarded but no reader token is stored, or Home
  Assistant rejects the reader token (for example, you revoked it): `up` fails. Reset with
  `docker compose down --volumes`, which also deletes Home Assistant and its history, or use
  [your own Home Assistant](#use-your-own-home-assistant) with a token you create.

Setup waits up to five minutes for Home Assistant to start. It logs when it
starts waiting and includes the last connection error or HTTP status if it
times out. Check it with `docker compose logs ha-setup`.

### Connect your devices

1. Print the login with the command above.
2. Open <http://127.0.0.1:8123> and log in as `owner`.
3. Go to **Settings → Devices & services → Add integration** and add your
   devices. For Philips Hue, use the [Hue integration](https://www.home-assistant.io/integrations/hue/)
   and enter the bridge IP address from the Hue app.
4. Open <http://127.0.0.1:8000>. The **Setup** tab lists the devices Home Assistant has now.

Setup is not specific to Hue. You can add any integration Home Assistant
supports. Home Keeper can monitor any single device from any brand: lights, smart
plugs and switches, other on/off devices such as fans and door sensors, and any
other device, such as a thermostat. Groups, rooms,
people, settings and other entities appear under **Show other entities**. When no supported device exists
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

### Switch lights

```sh
docker compose --profile control up --build --wait
```

The controller uses the control user's token, never the owner's. To stop
control without stopping the reader, stop the controller, or delete the
"Home Keeper control" user's token in Home Assistant under that user's
profile. [control.md](control.md) covers the commands.

### Stop and reset

```sh
# Stop, keeping Home Assistant with its history, its token and your monitored devices:
docker compose down
# Delete everything, including your Home Assistant setup:
docker compose down --volumes
```

## History retention

Home Keeper's History and usage numbers come from Home Assistant's
[recorder](https://www.home-assistant.io/integrations/recorder/). It keeps 10
days of history by default, so History cannot reach further back. To keep more,
set `purge_keep_days` in Home Assistant's `configuration.yaml`:

```yaml
recorder:
  purge_keep_days: 30
```

Restart Home Assistant for the change to take effect. A longer history makes
Home Assistant's database larger. With the bundled Home Assistant, the file is
in the `home-assistant-config` volume; edit it in a shell in the container, for
example `docker compose exec homeassistant vi /config/configuration.yaml`, then
`docker compose restart homeassistant`.

Home Keeper keeps no copy of device history, so this one setting decides how far
back History and usage go.

Each history read asks Home Assistant for the lights, plugs and switches in one
request, and for every other device in a request of its own. One answer can be at
most 16 MiB. A device that changes very often, such as a busy power sensor, can
exceed that over a long range. Then the whole read fails with `invalid_data`, and
History and usage cannot load for that range. Choose a shorter range or stop
monitoring that device.

## Use your own Home Assistant

`compose.external.yaml` replaces the bundled Home Assistant and `ha-setup` with
a Home Assistant you already run. Container `localhost` is not the computer
running Docker. Read [networking.md](networking.md) before you pick the address.

### Create a token

Create a dedicated Home Assistant user with permission to read the required
registries and selected entities, then generate a long-lived access token from
that user's profile. Home Assistant tokens do not provide a per-token read-only
entity scope. Treat the token as a powerful credential. The reader restricts
its own operations to fixed discovery, state reads and history reads of the
devices the API asks for, and a reachability check. Its WebSocket requests are
limited to `config/entity_registry/list`, `config/device_registry/list`,
`get_config`, `frontend/get_translations` and `history/history_during_period`.

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
export HOME_KEEPER_HA_URL=https://home-assistant.example.test:8123
export HOME_KEEPER_HA_TOKEN_FILE=/absolute/path/to/ha-token
# Optional: the address your browser uses, for the dashboard's link to Home Assistant.
export HOME_KEEPER_HA_PUBLIC_URL=https://home-assistant.example.test:8123
docker compose -f compose.yaml -f compose.external.yaml up --build --wait
```

You can put these lines in `.env` instead, without `export`; `.env.example` lists them.
Use your actual Home Assistant origin, without credentials, a query string or an
API path. The address above is an example. HTTPS certificate verification stays
on. Install a certificate trusted by the container; with an invalid certificate,
Home Keeper reports Home Assistant as unreachable. HTTP is supported for a trusted local network, but does not encrypt
the token in transit.

Use both Compose files for every command, for example
`docker compose -f compose.yaml -f compose.external.yaml down`.

### Turn on light control

Light control needs its own token, for a second Home Assistant user that is not
an administrator. Then you can revoke control without breaking the reader, and
Home Assistant's logbook names that user for every command.

1. In Home Assistant, open **Settings → People → Users** (turn on advanced mode
   in your profile if the tab is missing) and add a user, for example
   "Home Keeper control". Leave **Administrator** off.
2. Log in as that user, open its profile, and on the **Security** tab create a
   long-lived access token.
3. Save the token alone in a file outside this repository, with the same care
   and permissions as the reader's token above.
4. Start with the `control` profile:

```sh
export HOME_KEEPER_HA_CONTROL_TOKEN_FILE=/absolute/path/to/ha-control-token
docker compose -f compose.yaml -f compose.external.yaml --profile control up --build --wait
```

Keep `HOME_KEEPER_HA_URL` and `HOME_KEEPER_HA_TOKEN_FILE` set as above. When
`HOME_KEEPER_HA_CONTROL_TOKEN_FILE` is missing, the controller cannot start and
Docker reports the placeholder path `/nonexistent/set-HOME_KEEPER_HA_CONTROL_TOKEN_FILE`.

## Discover and select devices

Open <http://127.0.0.1:8000>. The header identifies Home Assistant mode. Discovery
shows supported devices by default, using their device names, under **Lights**,
**Plugs and switches**, **Other on/off** and **Other**. An on/off or other device
that has its own entity name adds it to the device name, such as
"Front door · Door". Room groups, people, settings and other
entities stay in the collapsed **Show other entities** section on the **Setup**
tab. Type in **Search devices** to show only entities whose name or device name
contains the text, in every section. Clear the search to see the full list again.
Discovery retains minimal device/entity metadata exposed by Home Assistant.

The catalog marks each entity with its `device_kind`:

- `light`: any enabled `light.*` entity that is not a group, whatever integration
  provides it.
- `switch`: a smart plug or switch, a `switch.*` entity. One can be monitored
  when it is enabled, belongs to a device, has no entity category and is not a
  group. Integrations also expose settings as switches, such as a plug's child
  lock or LED indicator. Home Assistant gives those an entity category of
  `config` or `diagnostic`, so they are left out.
- `on_off`: another device that is only on or off: a fan (`fan.*`), a binary
  sensor such as a door, motion or moisture sensor (`binary_sensor.*`), a siren,
  a humidifier or a remote. Home Keeper shows its state in Home Assistant's own
  words, such as "Open" for a door sensor that is on, with a plug's strip and
  usage numbers. It cannot switch it.
- `state`: any other device, such as a thermostat (`climate.*`), a cover or a
  text sensor. Home Keeper shows its state, in Home Assistant's own words, and
  its history of changes. It cannot switch it, and it has no usage numbers.

A device monitored as `state` before Home Keeper knew its domain as `on_off`
becomes an `on_off` device at the next catalog read, without selecting it again.

An entity of any other domain is an `on_off` or `state` device when Home
Assistant's registry shows that it is a device the household uses: it is enabled, belongs to
a device, has no entity category, is not hidden and is not a group. People
(`person.*`) and trackers (`device_tracker.*`) are never monitored.

Discovery reads every state once (`GET /api/states`) to detect groups and to
read each device's `device_class`, which picks its translated state words. It
keeps only which candidates are groups and the device classes. A device is a
group when it comes from the
[`group` integration](https://www.home-assistant.io/integrations/group/), when its
state lists member entities in an `entity_id` attribute, or when an attribute such
as `is_hue_group` or `is_deconz_group` is true. A Hue room is one example.
The dashboard gives the reason for each entity it cannot monitor:

- **Groups and rooms are not recorded**: a group or room.
- **Disabled in Home Assistant**: the entity is disabled there.
- **Settings and diagnostic switches are not recorded**: a switch with an
  entity category.
- **Switches without a device are not recorded**: a switch that belongs to no
  device, such as a helper.
- **Presence is not recorded**: a person or device tracker.
- **Settings and diagnostic entities are not recorded**: any other entity with
  an entity category.
- **Hidden in Home Assistant**: the entity is hidden there.
- **Only entities of a device are recorded**: any other entity that belongs to
  no device, such as a helper or a template sensor.

For a plug or switch, Home Keeper shows its on/off state and availability only,
with no light attributes. With the `control` profile it can switch it
on or off. For an `on_off` device it keeps the on/off state and availability, and
no attributes. For a `state` device it keeps the state text, at most 255
characters, and no attributes.

A candidate with no state yet is not a group. If the state read fails or returns
malformed data, discovery fails rather than guessing, and the next read tries
again.

### State words

Every reading carries a `label`: Home Assistant's own word for the state, such
as "Heat" for a thermostat in `heat`, else the raw state. Discovery reads Home
Assistant's language with `get_config`, then its translated state words with
`frontend/get_translations` (category `entity_component`). It looks up the
device's `device_class` first, then the words its domain uses for every device
class. The words are cached with the device list. If Home Assistant refuses the
language read, Home Keeper asks for English. Both reads together get at most 2
seconds, so a slow answer cannot delay the device list. If the translations
cannot be read in that time, discovery still succeeds and every label is the raw
state.

Discovery reads up to 5000 entity registry rows and 5000 device registry rows.
A home with more of either cannot load its device list: discovery fails with
`invalid_data`. You can monitor up to 500 devices at once. Selecting one more is
refused with `422 Selection is full`, and nothing is stored; stop monitoring a
device first.

Select a device to monitor it. Its current state and all the history Home
Assistant has for it appear at once. Devices you add to Home Assistant
appear on the **Setup** tab within seconds, without changing configuration or
restarting. Stable entity registry identity keeps selection and history across
an entity name change. A new registry entry reusing an old entity ID does not
inherit approval.

## Home and demo data

The home stack and the demo use separate Compose projects and volumes. The home
stack runs as project `home-keeper` and the demo as `home-keeper-demo`, whatever
the checkout folder is called. The API rejects a database written for the
other source. Stop one stack before starting the other, because both publish
the same localhost port.

## Check it with a real light

Test one single light end to end. The steps use a Hue light as the example:

1. Confirm the bulb appears and responds in Home Assistant.
2. Open Home Keeper, check the **HOME ASSISTANT** label, and select the bulb.
   Confirm the **Home** tab shows the state Home Assistant shows.
3. Change on/off and brightness in the Hue app. Leave each setting for about 20
   seconds. Confirm the changes appear on the **History** tab, at the same times
   as in Home Assistant's own history.
4. Stop Home Assistant with `docker compose stop homeassistant`. Confirm every
   tab shows "Can't reach Home Assistant" and the bulb's state is unknown.
5. Start it again with `docker compose start homeassistant`. Confirm the banner
   goes away and the state returns.
6. Restart the API with `docker compose restart api`. Confirm your selection
   remains and History still shows the changes from step 3.

When you report a failure, share the dashboard's error message and your Home
Assistant version. Never share the token or raw registry payloads.
