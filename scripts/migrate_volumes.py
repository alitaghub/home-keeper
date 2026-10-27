"""Copy a Home Observer install's Docker volumes to their Home Keeper names.

Run it with the old stack stopped and before the first `up` under the new name.
Standard library only, so it runs before `mise run setup`. See docs/upgrade.md.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_LABEL = "com.docker.compose.project"
VOLUME_LABEL = "com.docker.compose.volume"
# A small image with `cp -a`, which keeps ownership, modes and timestamps.
COPY_IMAGE = "alpine:3.22"


class MigrationError(Exception):
    """The migration cannot run; the message says why."""


def docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False)


def checked(*args: str) -> str:
    result = docker(*args)
    if result.returncode != 0:
        raise MigrationError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def compose_project_name(folder: str) -> str:
    """The project name Compose derives from a folder name."""
    return re.sub(r"[^-_a-z0-9]", "", folder.lower())


def project_volumes(project: str) -> list[str]:
    output = checked(
        "volume", "ls", "--filter", f"label={PROJECT_LABEL}={project}", "--format", "{{.Name}}"
    )
    return sorted(name for name in output.splitlines() if name)


# Exit codes of the occupancy probe. A file name never matters, even " " or a newline:
# find prints the full path, so any entry makes its output non-empty.
EMPTY, OCCUPIED = 0, 3
PROBE = f'[ -z "$(find /volume -mindepth 1 -maxdepth 1 -print -quit)" ] || exit {OCCUPIED}'


def compose_labels(volume: str) -> tuple[str, str] | None:
    """The volume's Compose project and volume labels ("" when unset), or None if missing."""
    labels = f'{{{{index .Labels "{PROJECT_LABEL}"}}}}\t{{{{index .Labels "{VOLUME_LABEL}"}}}}'
    result = docker("volume", "inspect", "--format", labels, volume)
    if result.returncode != 0:
        return None
    project, _, name = result.stdout.rstrip("\n").partition("\t")
    return project, name


def holds_data(volume: str) -> bool:
    result = docker(
        "run", "--rm", "--network", "none", "-v", f"{volume}:/volume:ro", COPY_IMAGE,
        "sh", "-c", PROBE,
    )  # fmt: skip
    if result.returncode not in (EMPTY, OCCUPIED):
        raise MigrationError(f"cannot inspect {volume}: {result.stderr.strip()}")
    return result.returncode == OCCUPIED


def create_volume(volume: str, project: str, name: str) -> None:
    # The labels make Compose adopt the volume as its own.
    checked(
        "volume", "create",
        "--label", f"{PROJECT_LABEL}={project}", "--label", f"{VOLUME_LABEL}={name}",
        volume,
    )  # fmt: skip


def migrate(old: str, new: str) -> list[str]:
    """Copy each `<old>_<name>` volume to `<new>_<name>`; return one line per copy."""
    if old == new:
        raise MigrationError(f"--from and --to are both {old}")
    sources = project_volumes(old)
    if not sources:
        raise MigrationError(f"no volumes found for project {old}")
    prefix = f"{old}_"
    unexpected = [source for source in sources if not source.startswith(prefix)]
    if unexpected:
        raise MigrationError(f"volumes without the {prefix} prefix: {', '.join(unexpected)}")
    pairs = [(source, source.removeprefix(prefix)) for source in sources]
    # Check every target before changing anything, so a refusal leaves no partial copy.
    missing: list[str] = []
    relabel: list[str] = []
    occupied: list[str] = []
    for _, name in pairs:
        target = f"{new}_{name}"
        labels = compose_labels(target)
        if labels is None:
            missing.append(target)
        elif holds_data(target):
            occupied.append(target)
        elif labels != (new, name):
            relabel.append(target)
    if occupied:
        raise MigrationError(f"target volumes already hold data: {', '.join(occupied)}")
    copied: list[str] = []
    for source, name in pairs:
        target = f"{new}_{name}"
        if target in relabel:
            # Docker cannot change labels, and Compose adopts a volume only by its labels,
            # so an empty mislabelled target is recreated. Nothing in it is lost.
            checked("volume", "rm", target)
        if target in missing or target in relabel:
            create_volume(target, new, name)
        checked(
            "run", "--rm", "--network", "none",
            "-v", f"{source}:/from:ro", "-v", f"{target}:/to",
            COPY_IMAGE, "cp", "-a", "/from/.", "/to/",
        )  # fmt: skip
        copied.append(f"copied {source} -> {target}")
    return copied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--from",
        dest="old",
        default=compose_project_name(Path.cwd().name),
        help="old Compose project name (default: the current folder's name)",
    )
    parser.add_argument("--to", dest="new", default="home-keeper", help="new project name")
    args = parser.parse_args(argv)
    old: str = args.old
    new: str = args.new
    try:
        for line in migrate(old, new):
            print(line)
    except MigrationError as error:
        print(f"error: {error}")
        return 1
    print(f"Done. The {old}_* volumes are unchanged; delete them once {new} works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
