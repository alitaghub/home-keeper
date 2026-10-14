FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

# Builds the dashboard; the runtime image below never contains Node.
FROM node:24.20.0-slim AS web
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/
RUN corepack enable && pnpm install --frozen-lockfile
COPY apps/web ./apps/web
RUN pnpm --filter @home-observer/web build

FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/observer/pyproject.toml ./apps/observer/
# uv needs every workspace member's manifest to read the lockfile; only the observer installs.
COPY apps/agent/pyproject.toml ./apps/agent/
COPY apps/observer/src ./apps/observer/src
# The API serves the dashboard from ./web relative to the working directory.
COPY --from=web /src/apps/web/dist ./web
RUN uv sync --locked --no-dev --no-editable --package home-observer
ENV PATH="/app/.venv/bin:$PATH"
# Shared secret and data volumes use a fixed, unprivileged identity.
RUN groupadd --gid 10001 observer && useradd --uid 10001 --gid 10001 observer \
    && mkdir /data /secrets /ha-token /ha-owner \
    && chown observer:observer /data /secrets /ha-token /ha-owner
USER 10001:10001
CMD ["python", "-m", "home_observer.runtime"]
