# Networking

## Networks

| Network          | Members                                                         | Internal |
| ---------------- | --------------------------------------------------------------- | -------- |
| `dashboard`      | `web`, `api`, `agent`                                           | No       |
| `collection`     | `api`, `reader`                                                 | Yes      |
| `control`        | `api`, `controller` (and the demo reader in the demo)           | Yes      |
| `database`       | `postgres`, `api`, `agent`, `telegram`                          | Yes      |
| `home-assistant` | `homeassistant`, `ha-setup`, `reader`, `controller`             | No       |
| `default`        | `agent`, `telegram`, for outbound access to the model provider and Telegram | No |

`db-setup` joins no network (`network_mode: none`). It reaches Postgres only
through the Unix socket in the `postgres-socket` volume.

The API never joins `home-assistant` and cannot resolve the `homeassistant`
name.

## Ports

Every published port binds to `127.0.0.1`.

| Port | Variable           | Service          |
| ---- | ------------------ | ---------------- |
| 8000 | `HOME_KEEPER_PORT` | `web` (dashboard) |
| 8123 | `HA_PORT`          | `homeassistant` (bundled only) |
| 2026 | `AGENT_PORT`       | `agent` (profile `agent`) |

## Bundled Home Assistant

Home Assistant runs on a bridge network, not host networking. mDNS and
multicast discovery may not see your LAN.

- Some integrations find devices another way. In testing, Hue found its bridge
  through Philips' online lookup.
- Otherwise, add the integration by IP address, such as the Hue Bridge IP from
  the Hue app. The container must be able to reach that address.
- USB and Bluetooth are not available.

## Your own Home Assistant

Set `HOME_KEEPER_HA_URL` to an address the reader container can reach.
Container `localhost` is the container, not your computer.

| Platform                     | Home Assistant on the same computer           | Home Assistant on another machine |
| ---------------------------- | --------------------------------------------- | --------------------------------- |
| Windows with WSL2            | `host.docker.internal`                        | Its LAN address or DNS name       |
| macOS with Docker Desktop    | `host.docker.internal`                        | Its LAN address or DNS name       |
| Linux with Docker Engine     | A LAN address, or a host-gateway mapping you add | Its LAN address or DNS name    |

- On WSL2, run Compose inside WSL with Docker Desktop's WSL integration on.
  Firewall, VPN and LAN routing can block the connection.
- On macOS, containers run inside a VM. LAN DNS, firewall and VPN rules still
  apply.
- On Linux, Home Assistant must listen on an address the container network can
  reach.
