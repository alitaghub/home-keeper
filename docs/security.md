# Security boundary

- The Observer API publishes only on `127.0.0.1`. Its API and collector run as an
  unprivileged user, without Linux capabilities, with read-only filesystems
  except their mounted volumes. The bundled Home Assistant also publishes only a
  loopback port and runs with its upstream container identity, without
  privileged mode, host networking, USB or Bluetooth access.
- Only the live collector mounts the Home Assistant token, read-only. The API,
  SQLite and browser never receive it. In the bundled stack, `ha-setup` writes
  the token with mode `600` to the `ha-token` volume. It keeps the Home
  Assistant owner login, also mode `600`, in the `ha-owner` volume that no other
  service mounts. Setup logs the owner password once; treat `docker compose logs`
  output as sensitive. Whole-home registries stay inside the trusted
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
  needed by a trusted credential service. No model can use this service as a
  tool. A future agent needs a separate network boundary and approved evidence
  tools before it can be added.

Do not expose the dashboard publicly. It has no user authentication. This version
makes no model calls, so no data leaves the home for a cloud model. A future cloud
investigation mode must document the evidence it sends before enabling it.

See [architecture.md](architecture.md) for how these parts connect, and
[home-assistant.md](home-assistant.md) for how the token is created and stored.
