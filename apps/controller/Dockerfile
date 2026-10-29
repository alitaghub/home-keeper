FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

# The light controller, a separate image so the collector image holds no control code.
# Build from the repository root:
#   docker build -f apps/controller/Dockerfile .
FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
# uv needs every workspace member's manifest to read the lockfile; the controller installs with the shared packages.
COPY apps/keeper/pyproject.toml ./apps/keeper/
COPY apps/agent/pyproject.toml ./apps/agent/
COPY apps/controller/pyproject.toml ./apps/controller/
COPY packages/light-command/pyproject.toml ./packages/light-command/
COPY packages/light-command/src ./packages/light-command/src
COPY packages/settings/pyproject.toml ./packages/settings/
COPY packages/settings/src ./packages/settings/src
COPY apps/controller/src ./apps/controller/src
RUN uv sync --locked --no-dev --no-editable --package home-keeper-controller
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 controller && useradd --uid 10001 --gid 10001 controller \
    && mkdir /secrets /ha-control-token && chown controller:controller /secrets /ha-control-token
USER 10001:10001
CMD ["python", "-m", "home_keeper_controller.server"]
