"""Real-Docker proof of scripts/migrate_volumes.py using throwaway project names.

Run with `uv run python e2e/migrate_volumes_smoke.py`. It creates and removes only volumes
named after a random `hk-migrate-test-*` project, so it is safe next to a running stack.
"""

import subprocess
import sys
import uuid
from pathlib import Path

from pydantic import TypeAdapter

LABELS = TypeAdapter(dict[str, str] | None)
REPO = Path(__file__).resolve().parent.parent
SUFFIX = uuid.uuid4().hex[:8]
OLD = f"hk-migrate-test-old-{SUFFIX}"
NEW = f"hk-migrate-test-new-{SUFFIX}"
EMPTY = f"hk-migrate-test-none-{SUFFIX}"
OLD2 = f"hk-migrate-test-old2-{SUFFIX}"
NEW2 = f"hk-migrate-test-new2-{SUFFIX}"
OLD3 = f"hk-migrate-test-old3-{SUFFIX}"
NEW3 = f"hk-migrate-test-new3-{SUFFIX}"
OLD4 = f"hk-migrate-test-old4-{SUFFIX}"
NEW4 = f"hk-migrate-test-new4-{SUFFIX}"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def create_volume(project: str, name: str) -> None:
    docker(
        "volume",
        "create",
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        f"com.docker.compose.volume={name}",
        f"{project}_{name}",
    )


def create_volume_unlabelled(project: str, name: str) -> None:
    docker("volume", "create", f"{project}_{name}")


def volume_labels(volume: str) -> dict[str, str]:
    """Parse docker's label JSON (an object, or null when unlabelled) into a typed mapping."""
    out = docker("volume", "inspect", "--format", "{{json .Labels}}", volume).stdout
    return LABELS.validate_json(out) or {}


def in_volume(volume: str, script: str) -> str:
    return docker("run", "--rm", "-v", f"{volume}:/v", "alpine", "sh", "-c", script).stdout


def snapshot(volume: str) -> str:
    return in_volume(
        volume,
        "stat -c 'root %a %u:%g' /v; cd /v"
        " && find . -type f -exec stat -c '%n %a %u:%g' {} + | sort"
        " && find . -type f -exec sha256sum {} + | sort",
    )


def migrate(old: str, new: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "python", "scripts/migrate_volumes.py", "--from", old, "--to", new],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def project_volumes(project: str) -> set[str]:
    out = docker("volume", "ls", "--filter", f"label=com.docker.compose.project={project}", "-q")
    return set(out.stdout.split())


def volumes_named(project: str) -> set[str]:
    out = docker("volume", "ls", "-q", "--filter", f"name={project}_")
    return set(out.stdout.split())


def main() -> None:
    try:
        create_volume(OLD, "data")
        create_volume(OLD, "ha-token")
        in_volume(
            f"{OLD}_data",
            "echo history > /v/home.sqlite && chmod 640 /v/home.sqlite"
            " && chown 1234:1234 /v/home.sqlite"
            # Hidden, nested state, like Home Assistant's .storage directory.
            " && mkdir /v/.storage && echo auth > /v/.storage/auth && chmod 600 /v/.storage/auth"
            " && chown 10001:10001 /v && chmod 700 /v",
        )
        in_volume(f"{OLD}_ha-token", "echo secret-token > /v/token && chmod 600 /v/token")
        before = {n: snapshot(f"{OLD}_{n}") for n in ("data", "ha-token")}

        first = migrate(OLD, NEW)
        assert first.returncode == 0, first.stderr + first.stdout
        assert project_volumes(NEW) == {f"{NEW}_data", f"{NEW}_ha-token"}
        for name in ("data", "ha-token"):
            labels = volume_labels(f"{NEW}_{name}")
            assert labels.get("com.docker.compose.project") == NEW, labels
            assert labels.get("com.docker.compose.volume") == name, labels
            assert snapshot(f"{NEW}_{name}") == before[name], f"{name} differs after copy"
            assert snapshot(f"{OLD}_{name}") == before[name], f"source {name} changed"
        assert "640 1234:1234" in snapshot(f"{NEW}_data")
        assert "./.storage/auth 600" in snapshot(f"{NEW}_data"), "hidden nested file lost"
        assert "root 700 10001:10001" in snapshot(f"{NEW}_data"), "root owner or mode lost"
        assert len(first.stdout.strip().splitlines()) >= 2, first.stdout

        second = migrate(OLD, NEW)
        assert second.returncode == 1, second.stderr + second.stdout
        assert {n: snapshot(f"{NEW}_{n}") for n in ("data", "ha-token")} == before
        assert {n: snapshot(f"{OLD}_{n}") for n in ("data", "ha-token")} == before

        missing = migrate(EMPTY, NEW)
        assert missing.returncode == 1, missing.stderr + missing.stdout
        assert f"no volumes found for project {EMPTY}" in missing.stdout + missing.stderr
        # The service user must be able to write into the copied volume root.
        docker(
            "run", "--rm", "--user", "10001:10001", "-v", f"{NEW}_data:/v", "alpine",
            "sh", "-c", "touch /v/probe && rm /v/probe",
        )  # fmt: skip

        # A later occupied target must stop the run before any earlier volume is copied.
        create_volume(OLD2, "data")
        create_volume(OLD2, "ha-token")
        in_volume(f"{OLD2}_data", "echo history > /v/home.sqlite")
        in_volume(f"{OLD2}_ha-token", "echo old-token > /v/token")
        create_volume(NEW2, "ha-token")
        in_volume(f"{NEW2}_ha-token", "echo precious > /v/token")
        occupied = snapshot(f"{NEW2}_ha-token")
        refused = migrate(OLD2, NEW2)
        assert refused.returncode == 1, refused.stderr + refused.stdout
        assert snapshot(f"{NEW2}_ha-token") == occupied, "occupied target changed"
        created = project_volumes(NEW2)
        assert f"{NEW2}_data" not in created or "home.sqlite" not in snapshot(f"{NEW2}_data"), (
            "data was copied before the occupied target was detected"
        )

        # A target holding only a file named " " is occupied, not empty.
        create_volume(OLD3, "data")
        in_volume(f"{OLD3}_data", "echo history > /v/home.sqlite")
        create_volume(NEW3, "data")
        in_volume(f"{NEW3}_data", "touch '/v/ '")
        space_only = snapshot(f"{NEW3}_data")
        blank = migrate(OLD3, NEW3)
        assert blank.returncode == 1, "a target with only a ' ' file was treated as empty"
        assert snapshot(f"{NEW3}_data") == space_only, "target with a ' ' file was overwritten"

        # An existing empty target without labels is adopted: it gets the Compose labels.
        create_volume_unlabelled(NEW4, "data")
        create_volume(OLD4, "data")
        in_volume(f"{OLD4}_data", "echo history > /v/home.sqlite")
        adopted = migrate(OLD4, NEW4)
        assert adopted.returncode == 0, adopted.stderr + adopted.stdout
        labels = volume_labels(f"{NEW4}_data")
        assert labels.get("com.docker.compose.project") == NEW4, labels
        assert labels.get("com.docker.compose.volume") == "data", labels
        assert snapshot(f"{NEW4}_data") == snapshot(f"{OLD4}_data"), "unlabelled target not filled"
        print("migrate_volumes smoke passed")
    finally:
        for project in (OLD, NEW, EMPTY, OLD2, NEW2, OLD3, NEW3, OLD4, NEW4):
            for volume in project_volumes(project) | volumes_named(project):
                docker("volume", "rm", "-f", volume, check=False)


if __name__ == "__main__":
    sys.exit(main())
