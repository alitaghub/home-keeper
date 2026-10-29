"""Compose keeps the Home Assistant owner token in the reader alone, with every profile on.

Reads `docker compose config`, so the checks see the merged files the way Compose runs them.
"""

import json
import os
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
PROFILES = ("control", "agent", "telegram")
# The owner token setup mints, and the setup state that can mint another.
OWNER_VOLUMES = {"ha-token", "ha-owner"}
EXTERNAL_ENV = {
    "HOME_KEEPER_HA_URL": "http://homeassistant.example:8123",
    "HOME_KEEPER_HA_TOKEN_FILE": "/run/home-keeper/ha-token",
    "HOME_KEEPER_HA_CONTROL_TOKEN_FILE": "/run/home-keeper/ha-control-token",
}


@cache
def config(*files: str) -> dict[str, Any]:
    docker = shutil.which("docker")
    assert docker, "docker compose is needed to read the Compose configuration"
    arguments = [docker, "compose"]
    for name in files:
        arguments += ["-f", str(REPO / name)]
    for profile in PROFILES:
        arguments += ["--profile", profile]
    result = subprocess.run(
        [*arguments, "config", "--format", "json"],
        cwd=REPO,
        env=os.environ | EXTERNAL_ENV,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def services(*files: str) -> dict[str, Any]:
    found: dict[str, Any] = config(*files)["services"]
    return found


def mounts(service: dict[str, Any]) -> list[dict[str, Any]]:
    volumes: list[dict[str, Any]] = service.get("volumes", [])
    return volumes


def on_network(found: dict[str, Any], network: str) -> set[str]:
    return {name for name, service in found.items() if network in service.get("networks", {})}


BUNDLED = ("compose.yaml",)
EXTERNAL = ("compose.yaml", "compose.external.yaml")
DEMO = ("compose.demo.yaml",)


def test_the_collector_is_now_the_reader_in_every_stack() -> None:
    for files in (BUNDLED, EXTERNAL, DEMO):
        found = services(*files)
        assert "reader" in found, files
        assert "collector" not in found, files


@pytest.mark.parametrize("files", [BUNDLED, EXTERNAL, DEMO], ids=["bundled", "external", "demo"])
def test_the_api_reaches_the_reader_and_never_home_assistant(files: tuple[str, ...]) -> None:
    found = services(*files)
    api = found["api"]
    assert api["environment"]["HOME_KEEPER_READER_URL"] == "http://reader:8080"
    assert "home-assistant" not in api.get("networks", {})
    # The API and the reader share an internal network, and the reader publishes no port.
    shared = set(api["networks"]) & set(found["reader"]["networks"])
    assert any(config(*files)["networks"][name].get("internal") for name in shared), shared
    assert not found["reader"].get("ports")


def test_only_the_reader_holds_the_owner_token_and_only_setup_can_mint_one() -> None:
    found = services(*BUNDLED)
    holders = {
        (name, volume["source"], volume["target"], volume.get("read_only", False))
        for name, service in found.items()
        for volume in mounts(service)
        if volume["source"] in OWNER_VOLUMES
    }
    assert holders == {
        ("ha-setup", "ha-token", "/ha-token", False),
        ("ha-setup", "ha-owner", "/ha-owner", False),
        ("reader", "ha-token", "/ha-token", True),
    }
    # The controller keeps its own non-admin token, read-only.
    assert {
        (volume["source"], volume.get("read_only", False))
        for volume in mounts(found["controller"])
        if volume["source"].startswith("ha-")
    } == {("ha-control-token", True)}
    # No other keeper container mounts any Home Assistant token.
    for name in found.keys() - {"ha-setup", "reader", "controller", "homeassistant"}:
        assert not any(volume["source"].startswith("ha-") for volume in mounts(found[name])), name


def test_only_the_reader_the_controller_and_setup_join_home_assistants_network() -> None:
    found = services(*BUNDLED)
    assert on_network(found, "home-assistant") == {
        "homeassistant",
        "ha-setup",
        "reader",
        "controller",
    }


def test_with_your_own_home_assistant_only_the_reader_mounts_the_owner_token() -> None:
    found = services(*EXTERNAL)
    token_mounts = {
        name
        for name, service in found.items()
        for volume in mounts(service)
        if volume["target"] == "/run/secrets/ha-token"
    }
    assert token_mounts == {"reader"}
    assert on_network(found, "home-assistant") == {"reader", "controller"}


@pytest.mark.parametrize("files", [BUNDLED, EXTERNAL, DEMO], ids=["bundled", "external", "demo"])
def test_the_reader_keeps_no_data(files: tuple[str, ...]) -> None:
    reader = services(*files)["reader"]
    targets = {volume["target"] for volume in mounts(reader)}
    assert "/data" not in targets
    assert not any(volume["source"] == "data" for volume in mounts(reader))
    assert reader.get("read_only") is True
