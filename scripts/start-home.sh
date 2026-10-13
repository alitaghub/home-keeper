#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo "Install Docker Engine with Compose first." >&2; exit 1; }
command -v curl >/dev/null || { echo "Install curl first." >&2; exit 1; }
docker compose version >/dev/null
docker compose -p home-observer-ha -f compose.home-assistant.yaml up -d
printf 'Waiting for local Home Assistant (up to five minutes)...\n'
ready=false
for ((attempt=0; attempt<150; attempt++)); do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8123/ >/dev/null; then
    ready=true
    break
  fi
  sleep 2
done
$ready || { echo "Home Assistant did not become ready; inspect its container logs locally." >&2; exit 1; }
exec bash scripts/connect-home.sh
