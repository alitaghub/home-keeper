FROM ghcr.io/astral-sh/uv:0.10.12 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY web ./web
RUN uv sync --locked --no-dev --no-editable
ENV PATH="/app/.venv/bin:$PATH"
# Shared secret and data volumes use a fixed, unprivileged identity.
RUN groupadd --gid 10001 observer && useradd --uid 10001 --gid 10001 observer \
    && mkdir /data /secrets && chown observer:observer /data /secrets
USER 10001:10001
CMD ["python", "-m", "home_observer.runtime"]
