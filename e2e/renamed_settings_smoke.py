"""A leftover OBSERVER_* variable must reach the service and stop `up` with the rename message.

Run with `uv run python e2e/renamed_settings_smoke.py`. It uses compose.demo.yaml under a
throwaway project name and its own port, so `down --volumes` removes only what it created and
never a real demo stack.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECT = "keeper-renamed-proof"
COMPOSE = ["docker", "compose", "-p", PROJECT, "-f", str(REPO / "compose.demo.yaml")]
EXPECTED = "OBSERVER_RETENTION_DAYS was renamed to HOME_KEEPER_RETENTION_DAYS"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*COMPOSE, *args], cwd=REPO, env=env, capture_output=True, text=True, check=False
    )


def main() -> None:
    env = {**os.environ, "HOME_KEEPER_PORT": "18004", "OBSERVER_RETENTION_DAYS": "30"}
    try:
        up = run("up", "--build", "--wait", env=env)
        assert up.returncode != 0, "up must fail while an old variable is set"
        logs = run("logs", "api", env=env)
        assert EXPECTED in logs.stdout + logs.stderr, logs.stdout + logs.stderr
        print("renamed settings smoke passed")
    finally:
        run("down", "--volumes", "--remove-orphans", env=env)


if __name__ == "__main__":
    sys.exit(main())
