import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from home_observer.api import create_app


def load_token(path: Path) -> str:
    return path.read_text().strip()


def create_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_token(path)
    with os.fdopen(descriptor, "w") as secret_file:
        secret_file.write(secrets.token_urlsafe(48))
    return load_token(path)


DEFAULT_HTTP_PORT = 80


def local_authorities(port: int, hosts: tuple[str, ...]) -> tuple[str, ...]:
    # Clients omit the default HTTP port from Host and Origin headers.
    if port == DEFAULT_HTTP_PORT:
        return hosts
    return tuple(f"{host}:{port}" for host in hosts)


def public_origin(value: str) -> str | None:
    """Parse the browser-facing Home Assistant origin; empty means none is configured."""
    if not value:
        return None
    error = ValueError("OBSERVER_HA_PUBLIC_URL must be an http(s) origin")
    parsed = urlsplit(value)
    try:
        _ = parsed.port  # Raises ValueError for an out-of-range or non-numeric port.
    except ValueError:
        raise error from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise error
    return f"{parsed.scheme}://{parsed.netloc}"


def main():
    token = create_token(Path(os.environ.get("OBSERVER_TOKEN_FILE", "/secrets/service-token")))
    port = int(os.environ.get("OBSERVER_PORT", "8000"))
    hosts = local_authorities(port, ("127.0.0.1", "localhost", "api"))
    app = create_app(
        db_path=Path(os.environ.get("OBSERVER_DB", "/data/home.sqlite")),
        service_token=token,
        source_id=os.environ.get("OBSERVER_SOURCE_ID", "demo"),
        home_assistant_url=public_origin(os.environ.get("OBSERVER_HA_PUBLIC_URL", "")),
        retention_days=int(os.environ.get("OBSERVER_RETENTION_DAYS", "30")),
        reconciliation_seconds=int(os.environ.get("OBSERVER_RECONCILIATION_SECONDS", "10")),
        allowed_hosts=hosts,
        allowed_origins=tuple(
            f"http://{authority}"
            for authority in local_authorities(port, ("127.0.0.1", "localhost"))
        ),
    )
    uvicorn.run(app, host=os.environ.get("OBSERVER_BIND", "127.0.0.1"), port=port, access_log=False)


if __name__ == "__main__":
    main()
