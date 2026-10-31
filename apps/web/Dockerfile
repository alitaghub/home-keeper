# The dashboard: built once, then served by Caddy, which also forwards /api to the keeper.
FROM node:24.20.0-slim AS build
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/
RUN corepack enable && pnpm install --frozen-lockfile
COPY apps/web ./apps/web
RUN pnpm --filter @home-keeper/web build

FROM caddy:2.11.7-alpine
# The image grants Caddy the low-port bind capability; it listens on 8080, and Compose
# drops every capability, so a binary that still asks for one would refuse to start.
RUN setcap -r /usr/bin/caddy
COPY apps/web/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /src/apps/web/dist /srv
# Caddy keeps its state under XDG paths; the container's root filesystem is read-only.
ENV XDG_CONFIG_HOME=/tmp/config XDG_DATA_HOME=/tmp/data
USER 10001:10001
EXPOSE 8080
