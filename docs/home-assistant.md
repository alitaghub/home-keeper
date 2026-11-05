# Home Assistant

Home Keeper reads every device through Home Assistant. It never talks to a
device, vendor or bridge directly.

You can run Home Keeper with a bundled Home Assistant, which it sets up for you,
or with a Home Assistant you already run.

## Bundled Home Assistant

### Start

```sh
docker compose up --build --wait
```

| Service         | Role                                                             | Port                                  |
| --------------- | ---------------------------------------------------------------- | ------------------------------------- |
| `homeassistant` | Home Assistant `2026.10.1`                                       | `127.0.0.1:8123` (`HA_PORT`)          |
| `ha-setup`      | Sets up Home Assistant, then exits                               | None                                  |
| `web`           | Serves the dashboard and forwards `/api` to the API              | `127.0.0.1:8000` (`HOME_KEEPER_PORT`) |
| `api`           | The Home Keeper API                                              | None                                  |
| `reader`        | Reads devices, states, history and statistics for the API        | None                                  |
| `postgres`      | Home Keeper's database                                           | None                                  |
| `db-setup`      | Creates a database and login for each service, then exits        | None                                  |
| `controller`    | Switches devices; only with `--profile control`                  | None                                  |

### Connect your devices

1. Print the login:

   ```sh
   docker compose run --rm --no-deps ha-setup login
   ```

2. Open <http://localhost:8123> and log in as `owner`.
3. Open **Settings → Devices & services → Add integration** and add your
   devices. For Philips Hue, add the
   [Hue integration](https://www.home-assistant.io/integrations/hue/) with the
   bridge IP address from the Hue app, then press the bridge button.
4. Open <http://localhost:8000>. The **Setup** tab lists the devices.

Any integration works. [devices.md](devices.md) lists which entities Home
Keeper can monitor.

### What setup does

`ha-setup` runs on every `up`. On the first run it onboards Home Assistant
through its HTTP API:

1. Creates the owner user `owner` with a random password.
2. Finishes onboarding with analytics off. Location, time zone and units keep
   Home Assistant's defaults.
3. Creates a long-lived token for the reader and stores it in the `ha-token`
   volume (mode `600`).
4. Stores the owner login in the `ha-owner` volume (mode `600`).
5. Creates the non-admin user `home-keeper-control` ("Home Keeper control")
   with a random password it does not keep. Stores that user's long-lived token
   in the `ha-control-token` volume (mode `600`).

Setup creates the control user even without the `control` profile, so you can
turn control on later with no extra step. Setup never writes a password or a
token to its log.

On later runs, setup checks what it finds:

| State                                                    | Result                                                                                         |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Both tokens stored and valid                             | Keeps the setup.                                                                               |
| Reader token valid, no control token                     | Creates the control user and its token with the reader's token. On failure, logs that control is off and retries on the next `up`. |
| Home Assistant rejects the control token                 | Logs that control is off and changes nothing. See [Restore control](#restore-control).         |
| Home Assistant not onboarded                             | Onboards it.                                                                                   |
| No reader token, or Home Assistant rejects it            | `up` fails. Reset with `docker compose down --volumes`, or [use your own Home Assistant](#use-your-own-home-assistant). |

Setup waits up to 5 minutes for Home Assistant to start. On timeout it logs the
last connection error or HTTP status. Check it with
`docker compose logs ha-setup`.

You can change the owner password in Home Assistant. The stored copy then goes
stale, but setup does not need it again.

### Restore control

After you revoke the control token, delete the stored file and run `up`:

```sh
docker compose run --rm --no-deps --entrypoint rm ha-setup /ha-control-token/token
docker compose --profile control up --build --wait
```

Setup creates a new token. The running controller reads it and control turns on
within 30 seconds.

### Limits

- Home Assistant runs on a Docker bridge network. mDNS and multicast discovery
  may not see your LAN; add an integration by IP address instead.
- USB and Bluetooth integrations are not available.
- Home Assistant runs with its upstream container identity, without privileged
  mode.
- Container installs have no Home Assistant apps. See the
  [installation guide](https://www.home-assistant.io/installation/linux/).
- Tested on Linux and WSL2 with Docker Engine, including a real Hue Bridge.
  macOS with Docker Desktop is untested.

## Use your own Home Assistant

`compose.external.yaml` replaces the bundled Home Assistant and `ha-setup`.

### 1. Create a reader token

1. In Home Assistant, create a dedicated user.
2. In that user's profile, on the **Security** tab, create a long-lived access
   token.
3. Save the token alone in a file outside this repository. Make it readable by
   container user `10001` only:

   ```sh
   sudo chown 10001:10001 /absolute/path/to/ha-token
   sudo chmod 600 /absolute/path/to/ha-token
   ```

> [!WARNING]
> A Home Assistant token cannot be limited to read-only access or to some
> entities. Treat it as a powerful credential. Never put it in `.env`, a URL, a
> command-line argument or a Compose environment variable.

The reader limits itself to these WebSocket requests:
`config/entity_registry/list`, `config/device_registry/list`, `get_config`,
`frontend/get_translations`, `history/history_during_period` and
`recorder/statistics_during_period`. It also calls `GET /api/states` and
`GET /api/`.

On Docker Desktop, file-sharing permissions can differ. Confirm that container
user `10001` can read the file and that it is not world-readable.

### 2. Start

Pick an address the reader container can reach. Container `localhost` is not
your computer; see [networking.md](networking.md).

```sh
export HOME_KEEPER_HA_URL=https://home-assistant.example.test:8123
export HOME_KEEPER_HA_TOKEN_FILE=/absolute/path/to/ha-token
export HOME_KEEPER_HA_PUBLIC_URL=https://home-assistant.example.test:8123  # optional
docker compose -f compose.yaml -f compose.external.yaml up --build --wait
```

You can put these values in `.env` instead, without `export`.

- `HOME_KEEPER_HA_URL` is an origin only: no credentials, query string or path.
- `HOME_KEEPER_HA_PUBLIC_URL` is the address your browser uses. The dashboard
  links to it.
- HTTPS certificate verification is always on. With a certificate the container
  does not trust, Home Keeper reports Home Assistant as unreachable.
- HTTP works, but does not encrypt the token. Use it only on a trusted network.

Pass both Compose files to every command, for example
`docker compose -f compose.yaml -f compose.external.yaml down`.

### 3. Turn on control (optional)

Control uses its own token for a second, non-admin user. You can then revoke
control without breaking the reader, and Home Assistant's logbook names that
user for every command.

1. Open **Settings → People → Users** and add a user, for example
   "Home Keeper control". Leave **Administrator** off. If the tab is missing,
   turn on advanced mode in your profile.
2. Log in as that user and create a long-lived access token.
3. Save it in a file with the same care as the reader token.
4. Start with the `control` profile:

   ```sh
   export HOME_KEEPER_HA_CONTROL_TOKEN_FILE=/absolute/path/to/ha-control-token
   docker compose -f compose.yaml -f compose.external.yaml --profile control up --build --wait
   ```

Without `HOME_KEEPER_HA_CONTROL_TOKEN_FILE`, the controller cannot start. Docker
reports the placeholder path `/nonexistent/set-HOME_KEEPER_HA_CONTROL_TOKEN_FILE`.

## History retention

History and usage come from Home Assistant's
[recorder](https://www.home-assistant.io/integrations/recorder/), which keeps
10 days by default. Home Keeper keeps no copy, so this setting alone decides how
far back History and usage reach.

To keep more, set `purge_keep_days` in Home Assistant's `configuration.yaml`:

```yaml
recorder:
  purge_keep_days: 30
```

Then restart Home Assistant. With the bundled Home Assistant:

```sh
docker compose exec homeassistant vi /config/configuration.yaml
docker compose restart homeassistant
```

A longer history makes Home Assistant's database larger.

Measurement charts use Home Assistant's statistics, which last longer:

| Statistics | Kept                                  | Used by                         |
| ---------- | ------------------------------------- | ------------------------------- |
| 5-minute   | As long as `purge_keep_days`          | 24-hour chart, card sparkline   |
| Hourly     | Until you delete them                 | 7-day and 30-day charts         |

Home Assistant writes a period's statistics after the period ends, so the
newest period appears a few minutes late.

### Read limits

Each history or statistics read shares one connection and these limits:

| Limit                          | Value     | Error          |
| ------------------------------ | --------- | -------------- |
| One answer                     | 16 MiB    | `invalid_data` |
| All answers of one read        | 64 MiB    | `invalid_data` |
| Whole read, connecting included | 8 seconds | `timeout`     |

A device that changes very often, such as a busy text sensor, can exceed the
size limits over a long range. Choose a shorter range or stop monitoring that
device.

## Check with a real light

Test one light end to end:

1. Confirm the light appears and responds in Home Assistant.
2. In Home Keeper, check the **HOME ASSISTANT** label and monitor the light.
   Confirm the **Home** tab shows the same state as Home Assistant.
3. Change on/off and brightness in the vendor app. Hold each setting for about
   20 seconds. Confirm the **History** tab shows each change at the same time as
   Home Assistant's history.
4. Run `docker compose stop homeassistant`. Confirm every tab shows "Can't
   reach Home Assistant" and the light's state is unknown.
5. Run `docker compose start homeassistant`. Confirm the banner goes away and
   the state returns.
6. Run `docker compose restart api`. Confirm the selection remains and History
   still shows the changes from step 3.

When you report a problem, include the dashboard's error message and your Home
Assistant version. Never share a token or raw registry data.
