"""Proves db-setup's isolation over the network: each service's role reaches only its own
database, and the bootstrap superuser password stops working.

Uses its own Compose project, and deletes its volumes at the end. COMPOSE_DB_FILE selects
another Compose file, for checking that a weakened db-setup fails this proof."""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FILE = os.environ.get("COMPOSE_DB_FILE", str(REPO / "compose.postgres.yaml"))
PROJECT = "hk-db-isolation-proof"


def compose(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", FILE, *args],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise SystemExit(f"docker compose {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
    return result.stdout


def password(name: str) -> str:
    """The password db-setup stored for the service `name`."""
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{PROJECT}_{name}-db-password:/p:ro",
            "postgres:18.6",
            "cat",
            "/p/password",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()


def connects(user: str, pw: str, db: str) -> bool:
    """Whether `user` can log in to `db` over the `database` network, as a service would."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            f"{PROJECT}_database",
            "-e",
            f"PGPASSWORD={pw}",
            "postgres:18.6",
            "psql",
            "-X",
            "-h",
            "postgres",
            "-U",
            user,
            "-d",
            db,
            "-c",
            "select 1",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.returncode == 0


def main() -> None:
    try:
        # Starts Postgres, waits until it is healthy, and fails with db-setup's exit code.
        compose("run", "--rm", "db-setup")
        failures: list[str] = []
        names = ["keeper", "agent", "telegram"]
        passwords = {name: password(name) for name in names}
        for name in names:
            if not connects(name, passwords[name], name):
                failures.append(f"{name} cannot reach its own database")
            others = [*[n for n in names if n != name], "postgres"]
            failures.extend(
                f"{name} reaches database {other}"
                for other in others
                if connects(name, passwords[name], other)
            )
        if connects("postgres", "replaced-by-db-setup", "postgres"):
            failures.append("bootstrap superuser password still works over TCP")
        assert not failures, failures
        print("Database isolation smoke passed: each role reaches only its own database")
    finally:
        compose("down", "-v")


if __name__ == "__main__":
    main()
