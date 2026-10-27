import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn

from home_keeper.api import create_app
from home_keeper_renamed import reject_renamed_variables


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


def parse_origin(variable: str, value: str) -> str | None:
    """Parse an http(s) origin from the environment; empty means none is configured."""
    if not value:
        return None
    error = ValueError(f"{variable} must be an http(s) origin")
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


def parse_timezone(variable: str, value: str) -> ZoneInfo:
    """Parse an IANA time zone name, such as Europe/Copenhagen, from the environment."""
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{variable} must be an IANA time zone name") from None


def main() -> None:
    reject_renamed_variables(os.environ)
    token = create_token(Path(os.environ.get("HOME_KEEPER_TOKEN_FILE", "/secrets/service-token")))
    port = int(os.environ.get("HOME_KEEPER_PORT", "8000"))
    hosts = local_authorities(port, ("127.0.0.1", "localhost", "api"))
    app = create_app(
        db_path=Path(os.environ.get("HOME_KEEPER_DB", "/data/home.sqlite")),
        service_token=token,
        source_id=os.environ.get("HOME_KEEPER_SOURCE_ID", "demo"),
        home_assistant_url=parse_origin(
            "HOME_KEEPER_HA_PUBLIC_URL", os.environ.get("HOME_KEEPER_HA_PUBLIC_URL", "")
        ),
        retention_days=int(os.environ.get("HOME_KEEPER_RETENTION_DAYS", "30")),
        reconciliation_seconds=int(os.environ.get("HOME_KEEPER_RECONCILIATION_SECONDS", "10")),
        timezone=parse_timezone(
            "HOME_KEEPER_TIMEZONE", os.environ.get("HOME_KEEPER_TIMEZONE", "UTC")
        ),
        allowed_hosts=hosts,
        allowed_origins=tuple(
            f"http://{authority}"
            for authority in local_authorities(port, ("127.0.0.1", "localhost"))
        ),
    )
    uvicorn.run(
        app, host=os.environ.get("HOME_KEEPER_BIND", "127.0.0.1"), port=port, access_log=False
    )


if __name__ == "__main__":
    main()
