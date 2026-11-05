# Security

Home Keeper is built for a single trusted computer. This page lists its trust
boundaries, where each credential lives, and what is not protected.

> [!WARNING]
> Do not expose the dashboard or the agent to a network. Neither has user
> authentication. With the `control` profile, anyone who can reach the API can
> switch your monitored devices.

## Credentials

| Credential                  | Created by  | Stored in                         | Read by                            |
| --------------------------- | ----------- | --------------------------------- | ---------------------------------- |
| Reader token (Home Assistant) | `ha-setup` or you | `ha-token` volume or your file, mode `600` | `reader`, read-only       |
| Control token (non-admin Home Assistant user) | `ha-setup` or you | `ha-control-token` volume or your file, mode `600` | `controller`, read-only |
| Home Assistant owner login  | `ha-setup`  | `ha-owner` volume, mode `600`     | `ha-setup` only                    |
| Service credential          | `api`       | `service-secret` volume           | `api`, `reader`, `controller`      |
| Database passwords          | `db-setup`  | One volume per service            | `db-setup` and that service        |
| Bot token, model keys       | You         | `.env`                            | `telegram`; `agent`                |

- No token or password is ever written to a log.
- The API, the database and the browser never receive a Home Assistant token.
- Revoking the control token stops control without breaking the reader. Home
  Assistant's logbook names the control user for every command.

## Read-only reader

The reader is the only service that reads Home Assistant. Its operations are
fixed:

| Operation                                                       | Purpose                                  |
| --------------------------------------------------------------- | ---------------------------------------- |
| `config/entity_registry/list`, `config/device_registry/list`    | Discovery                                |
| `GET /api/states`                                               | Groups, device classes and units at discovery; current states |
| `get_config`, `frontend/get_translations`                       | State words                              |
| `history/history_during_period`                                 | History of monitored devices             |
| `recorder/statistics_during_period`                             | Statistics of monitored measurements     |
| `GET /api/`                                                     | Reachability                             |

- No service calls, event subscriptions or device writes.
- No database access.
- It caches the projected catalog for up to 10 seconds. It never keeps a
  state, history or statistics.
- Registry data stays inside the reader until it reduces it to minimal
  metadata. Raw administrative URLs and unused registry IDs never reach the
  catalog or the database.
- The keeper image contains no controller code.

## Controller

The controller is the only code that can change a device. It runs only with
the `control` profile.

- It makes exactly four calls: `light.turn_on` (optional `brightness_pct`
  1–100) and `light.turn_off` on `light.*` IDs; `switch.turn_on` and
  `switch.turn_off`, with no data, on `switch.*` IDs.
- It rejects any other domain, service, entity ID or field, and a brightness
  for a switch, before it contacts Home Assistant.
- It has no HTTP passthrough, service-call passthrough or shell.
- A Home Assistant token cannot be limited to some entities. These fixed calls
  and the API's checks are the only limits on the control user.

The API forwards a command only when the device is monitored, is a light, plug
or switch, has a known available state that differs from the request, and the
controller's heartbeat is fresh. It records every forwarded command.

A command's `sender` is a label for the record, not authentication. Anything
that can call the API can send a command with any sender.

## API and dashboard

- Only `web` publishes a Home Keeper port, on `127.0.0.1`.
- `web` passes the browser's `Host` and `Origin` through unchanged. The API
  checks both on every write.
- Writes require the `X-Home-Keeper` header.
- Internal endpoints require the service credential.
- The Content Security Policy is
  `default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'`.
- Names from Home Assistant render as text, never as HTML.
- Schema validation limits every field. Errors and logs omit submitted secrets
  and raw upstream responses.

## Containers

- Every Home Keeper container drops all Linux capabilities and runs on a
  read-only filesystem, except its volumes.
- `api`, `reader` and `web` run as unprivileged users.
- The bundled Home Assistant runs with its upstream container identity, without
  privileged mode, host networking, USB or Bluetooth. It publishes only a
  loopback port.
- See [networking.md](networking.md) for which service joins which network.

## Database

- One Postgres serves the API, the agent and the Telegram worker. It publishes
  no port.
- `db-setup` gives each service its own login, random password and database.
  Each login can open only its own database.
- The superuser has no password and cannot log in over the network. `db-setup`
  reaches it through the Unix socket.

## Agent

The agent is optional (`--profile agent`).

- It calls only the public `/api/*` routes, with the same header as the
  dashboard.
- It cannot reach Home Assistant, a Home Assistant token, the keeper database
  or the service credential. It mounts only its own database password.
- Its only writes are a discovery refresh, selection changes and commands.
  Selection changes and commands wait for your approval.

Aegra, which serves the agent, has no authentication (`AUTH_TYPE=noop`):

- Anyone who can reach port 2026 or `/agent` can read and write every chat and
  run the agent. With `control`, they can also approve their own commands.
- Aegra's CORS allows no origin, so no other website can read its answers.
- `web` refuses `/agent` requests with `Sec-Fetch-Site` `cross-site` or
  `same-site`. Without this, a page you visit could post a form, because Aegra
  reads `text/plain` bodies as JSON.
- Port 2026 has no such check. Neither route checks `Host`, so DNS rebinding is
  not blocked.

## Telegram

The Telegram worker is optional and publishes no port.

- It accepts private messages only from listed numeric user IDs.
- Each change requires the requesting person's approval. Old or repeated
  buttons cannot approve another action.
- It receives its bot token, the allowlists, the agent's internal address and
  its own database password. It receives no Home Assistant token, model key, or
  access to the keeper or agent database.
- Its database stores polling progress, the bot ID, and each person's
  conversation reference and pending approvals. The agent keeps the
  conversation history.
- The allowlist does not add authentication to the agent panel or port 2026.

## What leaves your machine

| Destination      | When                       | What                                                                   |
| ---------------- | -------------------------- | ---------------------------------------------------------------------- |
| Model provider   | Profile `agent`            | Questions, earlier messages in the chat, and tool results: device names, states, timestamps, usage numbers, the control state and command outcomes |
| Telegram         | Profile `telegram`         | Questions and replies                                                  |

Without the `agent` profile, Home Keeper makes no model calls. The Home
Assistant token, the database and the service credential never leave.
