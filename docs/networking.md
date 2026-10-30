# Networking and platform limits

## Bundled Home Assistant

The default stack uses four Compose networks:

- `dashboard`: the API and the dashboard port.
- `collection`: internal; the API and the reader.
- `control`: internal; the API and, with `--profile control`, the controller.
- `home-assistant`: Home Assistant, `ha-setup`, the reader and the
  controller. The API never joins it and cannot even resolve the
  `homeassistant` name.

All published ports bind to `127.0.0.1` only: the dashboard on `HOME_KEEPER_PORT`
(default 8000) and Home Assistant on `HA_PORT` (default 8123).

Home Assistant runs on a bridge network, not host networking, so multicast and
mDNS discovery may not see your LAN. Some integrations still find devices
another way; Hue found its bridge through Philips' online lookup in testing. If
a device is not offered, add the integration by IP address, for example the Hue
Bridge IP from the Hue app. Home Assistant must be
able to reach that address from the container. USB and Bluetooth are not
available to it.

## Your own Home Assistant

Use an explicit address reachable from the reader container. Container
`localhost` means that container, not the computer running Docker.

- Windows/WSL2: run Compose in the WSL project with Docker Desktop WSL integration
  enabled. Use the dashboard's published localhost address. For Home Assistant
  on the Windows host, Docker Desktop provides `host.docker.internal`; for a
  separate home server, use its reachable LAN address or DNS name. WSL firewall,
  VPN and LAN routing can affect connectivity.
- macOS with Docker Desktop: `host.docker.internal` identifies the host. LAN DNS,
  firewall and VPN rules still apply. Docker Desktop runs containers inside a VM.
- Native Linux Docker Engine: `host.docker.internal` is not supplied automatically.
  Use a reachable LAN address, or configure a host-gateway mapping explicitly.
  Home Assistant must listen on an address reachable from the container network.
