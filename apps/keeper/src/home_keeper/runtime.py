import os
import secrets
import sys
from pathlib import Path

import uvicorn

from home_keeper.api import create_app
from home_keeper.control import HttpController
from home_keeper.reader import HttpReader
from home_keeper.settings import ApiSettings
from home_keeper.tokens import load_token
from home_keeper_settings import SettingsError, load


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


def main() -> None:
    try:
        settings = load(ApiSettings)
    except SettingsError as error:
        sys.exit(f"Home Keeper configuration invalid: {error}")
    token = create_token(settings.token_file)
    port = settings.port
    hosts = local_authorities(port, ("127.0.0.1", "localhost", "api"))
    app = create_app(
        db_path=settings.db,
        service_token=token,
        reader=HttpReader(settings.reader_url, token),
        source_id=settings.source_id,
        home_assistant_url=settings.ha_public_url,
        retention_days=settings.retention_days,
        timezone=settings.timezone,
        allowed_hosts=hosts,
        allowed_origins=tuple(
            f"http://{authority}"
            for authority in local_authorities(port, ("127.0.0.1", "localhost"))
        ),
        controller=(
            HttpController(settings.controller_url, token) if settings.controller_url else None
        ),
    )
    uvicorn.run(app, host=settings.bind, port=port, access_log=False)


if __name__ == "__main__":
    main()
