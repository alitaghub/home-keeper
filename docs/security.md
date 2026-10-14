# Security boundary

- The Observer API publishes only on `127.0.0.1`. Its API and collector run as an
  unprivileged user, without Linux capabilities, with read-only filesystems
  except their mounted volumes. The bundled Home Assistant also publishes only a
  loopback port and runs with its upstream container identity, without
  privileged mode, host networking, USB or Bluetooth access.
- Only the live collector reads the Home Assistant token at run time, from a
  read-only mount. The API,
  SQLite and browser never receive it. In the bundled stack, `ha-setup` writes
  the token with mode `600` to the `ha-token` volume. It keeps the Home
  Assistant owner login, also mode `600`, in the `ha-owner` volume that no other
  service mounts. Neither the token nor the password is written to a log.
  Whole-home registries stay inside the trusted
  connector until it projects minimal metadata. Raw administrative URLs and
  unused registry identifiers do not enter the catalog or database.
- The API creates an internal service credential in a restricted file. The
  collector mounts its volume read-only. Internal endpoints require it.
  Dashboard mutations require an explicit header and validate Host and Origin.
- Device and entity names from Home Assistant render as text, never as HTML,
  under a restrictive Content Security Policy. Schema validation limits fields
  and restricts source identity. Error status and logs omit submitted secrets
  and raw connector responses.
- Home Assistant operations are fixed registry reads, approved entity-state
  reads and a state-change subscription filtered inside the connector. There is no arbitrary HTTP passthrough, shell tool, router
  administration endpoint, service call or device-write operation.
- The demo collector uses an internal Docker network. The live collector also
  joins the `home-assistant` bridge network to reach Home Assistant. The API
  never joins that network. It has network access
  needed by a trusted credential service.
- The optional agent (`--profile agent`) is a separate container. It joins the
  dashboard network to reach the API, an internal network to reach its own
  Postgres, and the default network for egress to the model provider. It never
  joins the `home-assistant` network and mounts no observer volume, so it cannot
  reach Home Assistant, the token, the database or the service credential. It
  calls only the public `/api/*` routes, through the `api` host name, and sends
  the same mutation header as the dashboard. Its only writes are discovery
  refresh and selection changes, and a selection change waits for your approval
  in the chat. It runs read-only, without Linux capabilities.
- Aegra, which serves the agent, and the chat page publish only on `127.0.0.1`.
  The chat page calls Aegra directly, and Aegra's CORS allows only the chat
  origin built from `CHAT_PORT`. Agent Chat UI's own `/api` proxy has no target,
  because it allows every origin. Aegra runs without authentication
  (`AUTH_TYPE=noop`): anyone who can reach its port can read and write every
  chat thread and start the agent. CORS does not stop simple cross-site
  requests from a page you visit, and Aegra has no `Host` check, so DNS
  rebinding is not mitigated. Its Postgres publishes no port.

Do not expose the dashboard or the agent publicly. Neither has user
authentication. Without the agent profile, Home Observer makes no model calls.
With it, your questions and the agent's tool results go to the model provider
you configure in `AGENT_MODEL`. Tool results contain light names, states,
timestamps and usage numbers from the public API. The Home Assistant token, the
database and the service credential never leave. Chat threads stay in the local
Postgres volume, and the provider also receives each thread's earlier messages
as context. See [agent.md](agent.md).

See [architecture.md](architecture.md) for how these parts connect, and
[home-assistant.md](home-assistant.md) for how the token is created and stored.
