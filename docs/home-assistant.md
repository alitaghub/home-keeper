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

This starts four services:

- `homeassistant`: Home Assistant `2026.10.1`, published on
  <http://127.0.0.1:8123> (set `HA_PORT` to change the port).
- `ha-setup`: a one-shot container that sets up Home Assistant, then exits.
- `api`: the Home Keeper API and dashboard, published on <http://127.0.0.1:8000>
  (set `HOME_KEEPER_PORT` to change the port).
- `collector`: the live collector that reads Home Assistant.

With `--profile control`, a fifth service, `controller`, switches monitored
lights, plugs and switches. See [Switch lights](#switch-lights).

### What setup does

`ha-setup` completes Home Assistant onboarding through its HTTP API, with no
clicks:

1. It creates the owner user `owner` with a random password.
2. It finishes onboarding. Analytics stays disabled. Location, time zone and
   units keep Home Assistant's defaults; change them in Home Assistant.
3. It creates a long-lived token for the collector and stores it in the
   `ha-token` volume with mode `600`. Besides `ha-setup`, which writes and
   checks it, only the collector mounts that volume, read-only.
4. It stores the owner login with mode `600` in the `ha-owner` volume. Only
   `ha-setup` mounts that volume.
5. With the owner's collector token, it creates the user "Home Keeper control"
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
- The collector's token is stored but the control token is not, as in an
  install from before light control: it creates the control user and its token
  with the collector's token. It does not need the owner password. A control
  user left without a stored token, for example by an interrupted run, is
  replaced. If Home Assistant refuses, for example because another account uses
  the username `home-keeper-control`, setup still succeeds and collection
  starts. Setup logs that light control is off and tries again on the next `up`.
- Home Assistant rejects the stored control token (for example, you revoked
  it): setup still succeeds and collection starts. Setup logs that light control
  is off and changes nothing. To turn control on again, delete the control token
  file and run `up`:
  `docker compose run --rm --no-deps --entrypoint rm ha-setup /ha-control-token/token`.
  The running controller reads the new token, so control turns on within 30
  seconds without a restart.
- Home Assistant is not onboarded yet: it onboards it.
- Home Assistant is onboarded but no collector token is stored, or Home
  Assistant rejects the collector token (for example, you revoked it): `up` fails. Reset with
  `docker compose down --volumes`, which also deletes Home Keeper history, or use
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
4. Open <http://127.0.0.1:8000> and click **Refresh discovery**.

Setup is not specific to Hue. You can add any integration Home Assistant
supports. Home Keeper records any single light, smart plug or switch from any
brand. Groups, rooms and other entities appear under **Show other entities**. When no supported device exists
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
control without stopping collection, stop the controller, or delete the
"Home Keeper control" user's token in Home Assistant under that user's
profile. [control.md](control.md) covers the commands.

### Stop and reset

```sh
# Stop, keeping Home Assistant, its token and Home Keeper history:
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
export HOME_KEEPER_HA_URL=https://home-assistant.example.test:8123
export HOME_KEEPER_HA_TOKEN_FILE=/absolute/path/to/ha-token
# Optional: the address your browser uses, for the dashboard's link to Home Assistant.
export HOME_KEEPER_HA_PUBLIC_URL=https://home-assistant.example.test:8123
docker compose -f compose.yaml -f compose.external.yaml up --build --wait
```

You can put these lines in `.env` instead, without `export`; `.env.example` lists them.
Use your actual Home Assistant origin, without credentials, a query string or an
API path. The address above is an example. HTTPS certificate verification stays
on. Install a certificate trusted by the container; invalid certificates fail
collection. HTTP is supported for a trusted local network, but does not encrypt
the token in transit.

Use both Compose files for every command, for example
`docker compose -f compose.yaml -f compose.external.yaml down`.

### Turn on light control

Light control needs its own token, for a second Home Assistant user that is not
an administrator. Then you can revoke control without breaking collection, and
Home Assistant's logbook names that user for every command.

1. In Home Assistant, open **Settings → People → Users** (turn on advanced mode
   in your profile if the tab is missing) and add a user, for example
   "Home Keeper control". Leave **Administrator** off.
2. Log in as that user, open its profile, and on the **Security** tab create a
   long-lived access token.
3. Save the token alone in a file outside this repository, with the same care
   and permissions as the collector's token above.
4. Start with the `control` profile:

```sh
export HOME_KEEPER_HA_CONTROL_TOKEN_FILE=/absolute/path/to/ha-control-token
docker compose -f compose.yaml -f compose.external.yaml --profile control up --build --wait
```

Keep `HOME_KEEPER_HA_URL` and `HOME_KEEPER_HA_TOKEN_FILE` set as above. When
`HOME_KEEPER_HA_CONTROL_TOKEN_FILE` is missing, the controller cannot start and
Docker reports the placeholder path `/nonexistent/set-HOME_KEEPER_HA_CONTROL_TOKEN_FILE`.

## Discover and select lights

Open <http://127.0.0.1:8000>. The header identifies Home Assistant mode. Discovery
shows supported lamps by default, using their device names. Room groups, scenes,
sensors and other entities stay in the collapsed **Show other entities** section
on the **Setup** tab.
Discovery retains minimal device/entity metadata exposed by Home Assistant. Any
enabled `light.*` entity that is not a group can be monitored, whatever
integration provides it. Smart plugs and switches are `switch.*` entities. One
can be monitored when it is enabled, belongs to a device, has no entity category
and is not a group. Integrations also expose settings as switches, such as a
plug's child lock or LED indicator. Home Assistant gives those an entity
category of `config` or `diagnostic`, so they are left out.

Discovery reads the state of each new light or switch candidate once, to detect
groups, and keeps only that answer. It never reads the states of the whole home.
A light or switch is a group when it comes from the
[`group` integration](https://www.home-assistant.io/integrations/group/), when its
state lists member entities in an `entity_id` attribute, or when an attribute such
as `is_hue_group` or `is_deconz_group` is true. A Hue room is one example. The
catalog marks each light and switch with its `device_kind`: `light` or `switch`.
The dashboard gives the reason for each entity it cannot record:

- **Groups and rooms are not recorded**: a group or room light or switch.
- **Disabled in Home Assistant**: the entity is disabled there.
- **Settings and diagnostic switches are not recorded**: a switch with an
  entity category.
- **Switches without a device are not recorded**: a switch that belongs to no
  device, such as a helper.
- **Only lights, plugs and switches are supported**: any other entity, such as a
  sensor.

A plug or switch records its on/off state and availability only. Home Keeper
keeps no light attributes for it. With the `control` profile it can switch it
on or off.

A candidate with no state yet is not a group. If a candidate's state read fails
or returns malformed data, discovery fails rather than guessing, and the next
pass reads it again.

Select a light, plug or switch to record its first state. Add more devices to Home Assistant and
click **Refresh discovery** to find them without changing configuration or
restarting. Stable entity registry identity keeps selection and history across
an entity name change. A new registry entry reusing an old entity ID does not
inherit approval.

## Home and demo data

The home stack and the demo use separate Compose projects and volumes. The home
stack runs as project `home-keeper` and the demo as `home-keeper-demo`, whatever
the checkout folder is called. The API rejects a database containing
another source. Stop one stack before starting the other, because both publish
the same localhost port.

## Check it with a real light

Test one single light end to end. The steps use a Hue light as the example:

1. Confirm the bulb appears and responds in Home Assistant.
2. Open Home Keeper, check the **HOME ASSISTANT** label, and select the bulb.
   Wait for fresh evidence and a first baseline.
3. Change on/off and brightness in the Hue app. Leave each setting for about 20
   seconds. Confirm the changes appear on the **History** tab.
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
