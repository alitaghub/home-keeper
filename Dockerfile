FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

# Builds the dashboard; the runtime image below never contains Node.
FROM node:24.20.0-slim AS web
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/
RUN corepack enable && pnpm install --frozen-lockfile
COPY apps/web ./apps/web
RUN pnpm --filter @home-keeper/web build

FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/keeper/pyproject.toml ./apps/keeper/
# uv needs every workspace member's manifest to read the lockfile; the keeper installs with the shared packages.
# The controller is a manifest only: its code never enters this image.
COPY apps/agent/pyproject.toml ./apps/agent/
COPY apps/controller/pyproject.toml ./apps/controller/
COPY packages/light-command/pyproject.toml ./packages/light-command/
COPY packages/light-command/src ./packages/light-command/src
COPY packages/settings/pyproject.toml ./packages/settings/
COPY packages/settings/src ./packages/settings/src
COPY apps/keeper/src ./apps/keeper/src
# The API serves the dashboard from ./web relative to the working directory.
COPY --from=web /src/apps/web/dist ./web
RUN uv sync --locked --no-dev --no-editable --package home-keeper
ENV PATH="/app/.venv/bin:$PATH"
# Shared secret volumes use a fixed, unprivileged identity.
RUN groupadd --gid 10001 keeper && useradd --uid 10001 --gid 10001 keeper \
    && mkdir /secrets /ha-token /ha-owner /ha-control-token \
    && chown keeper:keeper /secrets /ha-token /ha-owner /ha-control-token
USER 10001:10001
CMD ["python", "-m", "home_keeper.runtime"]
