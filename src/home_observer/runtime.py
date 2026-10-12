import os
import secrets
from pathlib import Path

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


def main():
    token = create_token(Path(os.environ.get("OBSERVER_TOKEN_FILE", "/secrets/service-token")))
    port = int(os.environ.get("OBSERVER_PORT", "8000"))
    hosts = (f"127.0.0.1:{port}", f"localhost:{port}", f"api:{port}")
    app = create_app(
        db_path=Path(os.environ.get("OBSERVER_DB", "/data/home.sqlite")),
        service_token=token,
        reconciliation_seconds=int(os.environ.get("OBSERVER_RECONCILIATION_SECONDS", "10")),
        allowed_hosts=hosts,
        allowed_origins=(f"http://127.0.0.1:{port}", f"http://localhost:{port}"),
    )
    uvicorn.run(app, host=os.environ.get("OBSERVER_BIND", "127.0.0.1"), port=port, access_log=False)


if __name__ == "__main__":
    main()
