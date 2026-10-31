"""Prove light and plug control on the demo stack: off without the `control` profile, observed
with it.

Uses its own Compose project and port, and deletes its volumes at the end.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
PROJECT = "keeper-control-proof"
HOME_KEEPER_PORT = "18005"
KEEPER = f"http://127.0.0.1:{HOME_KEEPER_PORT}"
LIGHT = "light.hue_desk"
PLUG = "switch.smart_plug"
WRITE = {"X-Home-Keeper": "1", "Origin": KEEPER}
ON_37: dict[str, object] = {"action": "on", "brightness_pct": 37}
ON: dict[str, object] = {"action": "on"}
OFF: dict[str, object] = {"action": "off"}
# round(37 * 255 / 100); no demo replay phase is within two steps of it.
BRIGHTNESS_37 = 94
# Refusals that a demo replay phase can cause for a moment; anything else is a failure.
TRANSIENT = {"unavailable", "state_unknown", "already_in_state"}


def compose(
    *args: str, control: bool = True, check: bool = True
) -> subprocess.CompletedProcess[str]:
    profile = ["--profile", "control"] if control else []
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(REPO / "compose.demo.yaml"),
            *profile,
            *args,
        ],
        env=os.environ | {"HOME_KEEPER_PORT": HOME_KEEPER_PORT},
        cwd=REPO,
        check=check,
        capture_output=True,
        text=True,
        timeout=600,
    )


def wait_until[T](description: str, predicate: Callable[[], T], timeout: float = 60) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {description}")


def command(client: httpx.Client, action: dict[str, object], entity: str = LIGHT) -> httpx.Response:
    return client.post(
        f"/api/devices/{entity}/command",
        json={"command": action, "sender": {"kind": "dashboard"}},
        headers=WRITE,
    )


def reading(client: httpx.Client, entity: str = LIGHT) -> Any:
    """The state the demo home reports for `entity` now, or None."""
    for entry in client.get("/api/monitored").json()["entities"]:
        if entry["id"] == entity and entry["state"]:
            return entry["state"]
    return None


def current_state(client: httpx.Client, entity: str = LIGHT) -> Any:
    state = reading(client, entity)
    return state["state"] if state else None


def switch(
    client: httpx.Client, entity: str = LIGHT, on: dict[str, object] = ON_37
) -> tuple[dict[str, object], dict[str, Any]]:
    """Command the opposite of the device's state; retry while a replay phase refuses it."""

    def attempt() -> tuple[dict[str, object], dict[str, Any]] | None:
        action = OFF if current_state(client, entity) == "on" else on
        response = command(client, action, entity)
        if response.status_code == 409 and response.json()["detail"]["code"] in TRANSIENT:
            return None
        assert response.status_code == 200, response.text
        return action, response.json()

    accepted = wait_until(f"an accepted command on {entity}", attempt, timeout=45)
    assert accepted is not None
    return accepted


def assert_observed(
    client: httpx.Client, action: dict[str, object], record: dict[str, Any], entity: str = LIGHT
) -> None:
    assert record["outcome"] == "observed", record
    assert record["entity_id"] == entity
    assert record["device_kind"] == ("switch" if entity == PLUG else "light"), record
    assert record["command"] == action
    assert "event_id" not in record, record
    # Observed means the device's state, read after the send, showed the command.
    shown = reading(client, entity)
    assert shown["state"] == action["action"], shown
    if action is ON_37:
        assert abs(shown["brightness"] - BRIGHTNESS_37) <= 2, shown


def assert_in_history(
    client: httpx.Client, action: dict[str, object], record: dict[str, Any], entity: str = LIGHT
) -> None:
    """The commanded change shows in the device's history, which the API reads from the demo
    home's recorder."""
    since = datetime.fromisoformat(record["sent_at"]) - timedelta(seconds=5)

    def commanded(change: dict[str, Any]) -> bool:
        new: dict[str, Any] = change["new"] or {}
        if new.get("state") != action["action"]:
            return False
        brightness: int = new.get("brightness", -10)
        return action is not ON_37 or abs(brightness - BRIGHTNESS_37) <= 2

    def shown() -> bool:
        response = client.get(
            "/api/events", params={"entity_id": entity, "start": since.isoformat()}
        )
        assert response.status_code == 200, response.text
        return any(commanded(change) for change in response.json()["events"])

    wait_until(f"the commanded {action['action']} in the history of {entity}", shown, timeout=30)


def main() -> None:
    started = time.monotonic()
    try:
        config = json.loads(compose("config", "--format", "json").stdout)
        services = config["services"]
        published = sorted(name for name, service in services.items() if service.get("ports"))
        assert published == ["web"], published
        assert "controller" in services, "control profile adds no controller"
        controller_networks = set(services["controller"]["networks"])
        assert controller_networks == {"control"}, controller_networks
        assert "control" in services["api"]["networks"]
        assert "control" in services["reader"]["networks"]
        assert config["networks"]["control"].get("internal") is True

        # Without the profile nothing can switch a light.
        compose("up", "--build", "--wait", control=False)
        with httpx.Client(base_url=KEEPER, timeout=15, trust_env=False) as client:
            wait_until(
                "the demo catalog",
                lambda: LIGHT in {e["id"] for e in client.get("/api/catalog").json()["entities"]},
            )
            assert client.get("/api/control").json() == {"state": "off", "heartbeat_at": None}
            refused = command(client, ON_37)
            assert refused.status_code == 409, refused.text
            assert refused.json()["detail"]["code"] == "control_off"
            assert client.get("/api/commands").json()["commands"] == []

            compose("up", "--build", "--wait")
            wait_until("control on", lambda: client.get("/api/control").json()["state"] == "on")
            client.put(
                f"/api/selection/{LIGHT}", json={"selected": True}, headers=WRITE
            ).raise_for_status()
            wait_until("a state for the light", lambda: current_state(client))

            first_action, first = switch(client)
            assert_observed(client, first_action, first)
            assert_in_history(client, first_action, first)
            second_action, second = switch(client)
            assert second_action != first_action
            assert_observed(client, second_action, second)
            assert_in_history(client, second_action, second)
            audit = client.get("/api/commands").json()["commands"]
            assert [record["id"] for record in audit] == [second["id"], first["id"]]
            assert {record["sender"]["kind"] for record in audit} == {"dashboard"}

            # The demo plug switches through the same route, on and off with no brightness.
            client.put(
                f"/api/selection/{PLUG}", json={"selected": True}, headers=WRITE
            ).raise_for_status()
            wait_until("a state for the plug", lambda: current_state(client, PLUG))

            def dimmed_plug() -> bool:
                response = command(client, ON_37, PLUG)
                assert response.status_code == 409, response.text
                return response.json()["detail"]["code"] == "not_supported"

            wait_until("a plug brightness command refused as not supported", dimmed_plug)
            plug_actions: list[dict[str, object]] = []
            for _ in range(2):
                plug_action, plug_record = switch(client, PLUG, ON)
                assert_observed(client, plug_action, plug_record, PLUG)
                assert_in_history(client, plug_action, plug_record, PLUG)
                plug_actions.append(plug_action)
            assert plug_actions[0] != plug_actions[1]
            plug_audit = [
                record
                for record in client.get("/api/commands").json()["commands"]
                if record["entity_id"] == PLUG
            ]
            assert [record["command"] for record in plug_audit] == plug_actions[::-1]

            # A stopped controller: unreachable while its heartbeat is fresh, then control off.
            compose("kill", "controller")
            _, stopped = switch(client)
            assert stopped["outcome"] == "unreachable", stopped
            wait_until(
                "control off after the heartbeat goes stale",
                lambda: client.get("/api/control").json()["state"] == "off",
                timeout=45,
            )
            refused = command(client, ON_37)
            assert refused.status_code == 409, refused.text
            assert refused.json()["detail"]["code"] == "control_off"
    except BaseException:
        print(compose("logs", "--no-color", check=False).stdout[-20000:], file=sys.stderr)
        raise
    finally:
        compose("down", "--volumes", check=False)
    print(f"Control smoke passed in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
