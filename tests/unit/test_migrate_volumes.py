"""migrate_volumes refuses to change a target volume whose contents it could not check."""

import os
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import migrate_volumes
import pytest

Docker = Callable[..., subprocess.CompletedProcess[str]]
NOT_FOUND = "Error response from daemon: get home-keeper_data: no such volume"


def fake_docker(calls: list[tuple[str, ...]], *, inspect: str | None, probe: int) -> Docker:
    """A Docker daemon with one source volume; `inspect` is the target's error, if any."""

    def docker(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ("volume", "ls"):
            return subprocess.CompletedProcess(args, 0, "home-observer_data\n", "")
        if args[:2] == ("volume", "inspect"):
            # Mislabelled, so a probe that wrongly says EMPTY leads to `volume rm`.
            return subprocess.CompletedProcess(
                args, 1 if inspect else 0, "other\tdata\n", inspect or ""
            )
        if "sh" in args:
            return subprocess.CompletedProcess(args, probe, "", "probe failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    return docker


def changes(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [
        call
        for call in calls
        if call[:2] in (("volume", "create"), ("volume", "rm")) or "cp" in call
    ]


def test_failed_target_inspect_aborts_before_any_change(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    error = "Error response from daemon: context deadline exceeded"
    monkeypatch.setattr(migrate_volumes, "docker", fake_docker(calls, inspect=error, probe=0))

    with pytest.raises(migrate_volumes.MigrationError, match="context deadline exceeded"):
        migrate_volumes.migrate("home-observer", "home-keeper")

    assert changes(calls) == []


def test_missing_target_is_created_then_copied(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(migrate_volumes, "docker", fake_docker(calls, inspect=NOT_FOUND, probe=0))

    assert migrate_volumes.migrate("home-observer", "home-keeper") == [
        "copied home-observer_data -> home-keeper_data"
    ]
    assert [call[:2] for call in changes(calls)] == [("volume", "create"), ("run", "--rm")]


def test_failed_occupancy_probe_aborts_before_any_change(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(migrate_volumes, "docker", fake_docker(calls, inspect=None, probe=125))

    with pytest.raises(migrate_volumes.MigrationError, match="cannot inspect home-keeper_data"):
        migrate_volumes.migrate("home-observer", "home-keeper")

    assert changes(calls) == []


def test_probe_fails_instead_of_reporting_empty_when_find_fails(tmp_path: Path) -> None:
    # A `find` that errors, as on an unreadable volume, must not read as EMPTY.
    failing_find = tmp_path / "find"
    failing_find.write_text("#!/bin/sh\necho 'find: /volume: Permission denied' >&2\nexit 1\n")
    failing_find.chmod(failing_find.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        ["sh", "-c", migrate_volumes.PROBE],
        env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        check=False,
    )

    assert result.returncode not in (migrate_volumes.EMPTY, migrate_volumes.OCCUPIED)
