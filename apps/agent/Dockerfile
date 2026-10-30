FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

# Aegra serving the agent graph. Build from the repository root:
#   docker build -f apps/agent/Dockerfile .
FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
# uv needs every workspace member's manifest to read the lockfile; the agent installs with the shared package.
COPY apps/keeper/pyproject.toml ./apps/keeper/
COPY apps/agent/pyproject.toml ./apps/agent/
COPY apps/controller/pyproject.toml ./apps/controller/
COPY packages/light-command/pyproject.toml ./packages/light-command/
COPY packages/light-command/src ./packages/light-command/src
COPY packages/settings/pyproject.toml ./packages/settings/
COPY packages/settings/src ./packages/settings/src
COPY apps/agent/src ./apps/agent/src
RUN uv sync --locked --no-dev --no-editable --package home-keeper-agent --extra serve
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid 10001 agent
USER 10001:10001
# Writes Aegra's config for CHAT_ORIGIN to /tmp, then runs `aegra serve` on all interfaces;
# Compose publishes the port on 127.0.0.1 only.
EXPOSE 2026
CMD ["python", "-m", "home_keeper_agent.serve"]
