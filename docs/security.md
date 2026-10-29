# Security boundary

- The Home Keeper API publishes only on `127.0.0.1`. Its API and reader run as an
  unprivileged user, without Linux capabilities, with read-only filesystems
  except their mounted volumes. The bundled Home Assistant also publishes only a
  loopback port and runs with its upstream container identity, without
  privileged mode, host networking, USB or Bluetooth access.
- Only the reader reads the Home Assistant token at run time, from a
  read-only mount. The API,
  SQLite and browser never receive it. With the `control` profile, the
  controller reads a second token, for the non-admin "Home Keeper control"
  user, from the `ha-control-token` volume, read-only. Only `ha-setup`, which
  writes it with mode `600`, and the controller mount that volume. Revoke
  control in Home Assistant without breaking the reader, and its logbook names
  that user for every command. In the bundled stack, `ha-setup` writes
  the token with mode `600` to the `ha-token` volume. It keeps the Home
  Assistant owner login, also mode `600`, in the `ha-owner` volume that no other
  service mounts. Neither the token nor the password is written to a log.
  Whole-home registries stay inside the trusted
  connector until it projects minimal metadata. Raw administrative URLs and
  unused registry identifiers do not enter the catalog or database.
- The API creates an internal service credential in a restricted file. The
  reader mounts its volume read-only. Internal endpoints require it.
  Dashboard mutations require an explicit header and validate Host and Origin.
- Device and entity names from Home Assistant render as text, never as HTML,
  under a restrictive Content Security Policy. Schema validation limits fields
  and restricts source identity. Error status and logs omit submitted secrets
  and raw connector responses.
- The reader's Home Assistant operations are fixed registry reads, one state
  read for each new light or switch candidate at discovery, state reads of the
  lights and switches the API asks for, `history/history_during_period` for
  their history, and `GET /api/` to check that Home Assistant answers. Discovery reads a candidate's state only to detect a group
  and keeps only that answer. The reader keeps the projected catalog in memory
  for up to 10 seconds and never keeps a state or history. It makes no service call, no
  event subscription and no device write, and it has no `/data` mount. The keeper
  image contains no controller code.
- The controller makes exactly four Home Assistant service calls:
  `light.turn_on`, with an optional `brightness_pct` from 1 to 100, and
  `light.turn_off`, on `light.*` entity IDs, and `switch.turn_on` and
  `switch.turn_off`, with no data, on `switch.*` entity IDs. Each command names
  its device kind, and each kind accepts only its own entity IDs and commands.
  It rejects any other domain, service, entity ID or field, and a brightness for
  a switch, before it contacts Home Assistant. There is no
  arbitrary HTTP passthrough, service-call passthrough, shell tool or router
  administration endpoint. A Home Assistant token cannot be limited to some
  entities, so these fixed calls and the API's checks are the only limits on the
  control user.
- The API forwards a command only for a monitored light with a known, available
  state that differs from the request, and only while the controller's
  heartbeat is fresh. It records every forwarded command. A command's `sender`
  (dashboard, browser chat, or a Telegram ID and display name) is a label the
  caller reports for the record. It is not authentication: anything that can
  call the API can send a command with any sender. Sender names render as text.
- The demo reader uses internal Docker networks only. The live reader also
  joins the `home-assistant` bridge network to reach Home Assistant. It
  publishes no port. The API
  never joins that network. It has network access
  needed by a trusted credential service.
- The controller publishes no port. It joins the internal `control` network,
  shared only with the API (and, in the demo, the demo reader), and in the
  bundled stack the `home-assistant` network. Its command endpoint and the
  demo reader's need the service credential, which only the API, the
  reader and the controller hold. The controller sends its heartbeat to the
  API with the same credential.
- The optional agent (`--profile agent`) is a separate container. It joins the
  dashboard network to reach the API, an internal network to reach its own
  Postgres, and the default network for egress to the model provider. It never
  joins the `home-assistant` network and mounts no Home Keeper volume, so it cannot
  reach Home Assistant, the token, the database or the service credential. It
  calls only the public `/api/*` routes, through the `api` host name, and sends
  the same mutation header as the dashboard. Its only writes are discovery
  refresh, selection changes and light commands. A selection change or a light
  command waits for your approval in the chat. A light command goes through the
  API's own checks and the controller, like one from the dashboard. It runs
  read-only, without Linux capabilities.
- Aegra, which serves the agent, and the chat page publish only on `127.0.0.1`.
  The chat page calls Aegra directly, and Aegra's CORS allows only the chat
  origin built from `CHAT_PORT`. Agent Chat UI's own `/api` proxy has no target,
  because it allows every origin. Aegra runs without authentication
  (`AUTH_TYPE=noop`): anyone who can reach its port can read and write every
  chat thread and start the agent. With the `control` profile, they can also
  propose and approve their own light commands, because the approval prompt
  is answered on the same unauthenticated port. CORS does not stop simple cross-site
  requests from a page you visit, and Aegra has no `Host` check, so DNS
  rebinding is not mitigated. Its Postgres publishes no port.

Do not expose the dashboard or the agent publicly. Neither has user
authentication. With the `control` profile, anyone who can reach the API can
switch your monitored lights. Without the agent profile, Home Keeper makes no model calls.
With it, your questions and the agent's tool results go to the model provider
you configure in `AGENT_MODEL`. Tool results contain light names, states,
timestamps, usage numbers, the light control state and light command outcomes
from the public API. The Home Assistant token, the
database and the service credential never leave. Chat threads stay in the local
Postgres volume, and the provider also receives each thread's earlier messages
as context. See [agent.md](agent.md).

See [architecture.md](architecture.md) for how these parts connect, and
[home-assistant.md](home-assistant.md) for how the token is created and stored.

## Optional Telegram bot

The Telegram worker is opt-in and publishes no port. It accepts private messages
only from configured numeric Telegram user IDs. All allowed users can use the
same home tools; conversation history is separate, but monitoring choices affect
the shared home. Changing that choice requires the requesting user's approval.
Old or repeated callback buttons cannot approve another action.

Questions and replies pass through Telegram, in addition to the model provider's
existing access to questions and tool results. The bot receives only its bot
token, allowlist, internal agent address, and private state location. It receives
no Home Assistant token, model key, Home Keeper database, or Aegra database mount.
Its small persistent state stores Telegram progress, conversation references,
and pending approvals; Aegra keeps the actual conversation history.

The existing local agent API remains unauthenticated and loopback-only. Telegram
allowlisting does not add authentication to browser chat or the agent port.
See [Telegram setup and limits](telegram.md).
