# Networking and platform limits

Use an explicit address reachable from the collector container. Container
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

These are setup directions, not platform test results. The demo was verified on
WSL2/Linux kernel `6.18.33.2-microsoft-standard-WSL2`, Docker Engine `29.9.0`
`linux/amd64`, and Compose `5.6.0`. Native Windows, macOS and a separate native
Linux deployment have not been tested. Real Hue hardware and a user's Home
Assistant instance have not been tested.
