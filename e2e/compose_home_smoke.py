"""Boot the default stack with the real pinned Home Assistant image and prove setup needs no human.

Runs with the `control` profile, so setup also mints the controller's own token.

Also proves that History reads Home Assistant's real recorder: the proof sets a template
light's state a few times and reads the changes back through the reader and the API.

Uses its own Compose project and ports, and deletes its volumes at the end.
"""

import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from websockets.sync.client import connect

REPO = Path(__file__).resolve().parents[1]
PROJECT = "keeper-home-proof"
HOME_KEEPER_PORT = "18002"
HA_PORT = "18124"
KEEPER = f"http://127.0.0.1:{HOME_KEEPER_PORT}"
HOME_ASSISTANT = f"http://127.0.0.1:{HA_PORT}"
# A template light: a light in Home Assistant's registry with no hardware behind it.
PROOF_NAME = "Keeper history proof"
PROOF_LIGHT = f"""
template:
  - light:
      - name: {PROOF_NAME}
        unique_id: keeper_history_proof
        turn_on: []
        turn_off: []
"""
PROOF_ENTITY = "light.keeper_history_proof"
# The states the proof sets, in order: on, brightness only, color only, then off.
PROOF_STATES: list[dict[str, Any]] = [
    {"state": "on", "attributes": {"brightness": 100, "color_mode": "brightness"}},
    {"state": "on", "attributes": {"brightness": 200, "color_mode": "brightness"}},
    {
        "state": "on",
        "attributes": {"brightness": 200, "color_mode": "xy", "xy_color": [0.4, 0.3]},
    },
    {"state": "off", "attributes": {"friendly_name": PROOF_NAME}},
]
# What each state is as a Home Keeper reading, without its times.
PROOF_READINGS: list[dict[str, Any]] = [
    {"state": "on", "brightness": 100, "color_mode": "brightness"},
    {"state": "on", "brightness": 200, "color_mode": "brightness"},
    {"state": "on", "brightness": 200, "color_mode": "xy", "xy_color": [0.4, 0.3]},
    {"state": "off"},
]
# Asks the reader for history from inside its container, with the service credential only
# the keeper's containers hold.
READ_HISTORY = """
import sys
import httpx
token = open("/secrets/service-token").read().strip()
response = httpx.get(
    "http://127.0.0.1:8080/internal/history",
    params={"entity_id": sys.argv[1], "start": sys.argv[2], "end": sys.argv[3]},
    headers={"Authorization": f"Bearer {token}"},
    timeout=15,
)
print(response.status_code)
print(response.text)
"""


def compose(
    *args: str, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(REPO / "compose.yaml"),
            "--profile",
            "control",
            *args,
        ],
        env=os.environ | {"HOME_KEEPER_PORT": HOME_KEEPER_PORT, "HA_PORT": HA_PORT},
        cwd=REPO,
        check=check,
        capture_output=True,
        text=True,
        timeout=600,
        input=stdin,
    )


def wait_for_healthy_reader(client: httpx.Client) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if client.get("/api/health").json()["status"] == "healthy":
            return
        time.sleep(1)
    raise AssertionError("The reader never reported healthy against the bundled Home Assistant")


def wait_until_control_on(client: httpx.Client) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if client.get("/api/control").json()["state"] == "on":
            return
        time.sleep(1)
    raise AssertionError("Controller never reported control on against the bundled Home Assistant")


def log_in(username: str, password: str) -> str:
    """Run Home Assistant's own login form flow, as the browser does."""
    client_id = f"{HOME_ASSISTANT}/"
    with httpx.Client(base_url=HOME_ASSISTANT, timeout=10, trust_env=False) as client:
        flow = client.post(
            "/auth/login_flow",
            json={
                "client_id": client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": f"{client_id}?auth_callback=1",
            },
        ).json()
        return client.post(
            f"/auth/login_flow/{flow['flow_id']}",
            json={"client_id": client_id, "username": username, "password": password},
        ).json()["type"]


def current_user(token: str) -> Any:
    """Ask Home Assistant which user a token belongs to."""
    with connect(f"ws://127.0.0.1:{HA_PORT}/api/websocket") as socket:
        socket.recv()
        socket.send(json.dumps({"type": "auth", "access_token": token}))
        assert json.loads(socket.recv())["type"] == "auth_ok"
        socket.send(json.dumps({"id": 1, "type": "auth/current_user"}))
        reply = json.loads(socket.recv())
    assert reply["success"], reply
    return reply["result"]


def control_token() -> str:
    return compose(
        "run", "--rm", "--no-deps", "--entrypoint", "cat", "ha-setup", "/ha-control-token/token"
    ).stdout.strip()


def near(value: str, expected: str) -> bool:
    """Whether two Home Assistant times name the same instant. The recorder keeps Unix-second
    floats, which can differ from the state's own time in the last microsecond."""
    difference = datetime.fromisoformat(value) - datetime.fromisoformat(expected)
    return abs(difference) <= timedelta(milliseconds=1)


def proof_light(client: httpx.Client) -> str:
    """The template light's Home Keeper ID, once the reader's catalog lists it."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = client.get("/api/catalog")
        if response.status_code == 200:
            for entity in response.json()["entities"]:
                if entity["name"] == PROOF_NAME:
                    assert entity["supported"], entity
                    identity: str = entity["id"]
                    return identity
        time.sleep(1)
    raise AssertionError("The template light never appeared in the catalog")


def set_proof_states(owner_token: str) -> list[dict[str, Any]]:
    """Set each proof state through Home Assistant's REST API; return what it stored."""
    stored: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=HOME_ASSISTANT,
        headers={"Authorization": f"Bearer {owner_token}"},
        timeout=10,
        trust_env=False,
    ) as home_assistant:
        for state in PROOF_STATES:
            response = home_assistant.post(f"/api/states/{PROOF_ENTITY}", json=state)
            assert response.status_code in (200, 201), response.text
            stored.append(response.json())
            # Distinct times, so each state is its own recorder row.
            time.sleep(0.5)
    return stored


def reader_history(identity: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The reader's history of one device, read inside the reader container."""
    result = compose(
        "exec",
        "-T",
        "reader",
        "python",
        "-c",
        READ_HISTORY,
        identity,
        start.isoformat(),
        end.isoformat(),
    ).stdout.splitlines()
    assert result[0] == "200", result
    rows: list[dict[str, Any]] = json.loads(result[1])["history"][identity]
    return rows


def prove_history_comes_from_the_recorder(owner_token: str) -> None:
    """Set states on the template light, then read them back as recorder rows through the
    reader and as changes through the API, at Home Assistant's own times."""
    with httpx.Client(base_url=KEEPER, timeout=10, trust_env=False) as client:
        wait_for_healthy_reader(client)
        identity = proof_light(client)
        stored = set_proof_states(owner_token)
        start = datetime.fromisoformat(stored[0]["last_updated"]) - timedelta(seconds=1)
        end = datetime.now(UTC) + timedelta(minutes=1)

        # The recorder commits every few seconds; wait until it holds the last state.
        deadline = time.monotonic() + 60
        rows = reader_history(identity, start, end)
        while not any(
            near(row["reading"]["last_updated"], stored[-1]["last_updated"]) for row in rows
        ):
            assert time.monotonic() < deadline, rows
            time.sleep(2)
            rows = reader_history(identity, start, end)

        for state, reading in zip(stored, PROOF_READINGS, strict=True):
            [row] = [r for r in rows if near(r["reading"]["last_updated"], state["last_updated"])]
            assert row["source_entity_id"] == PROOF_ENTITY, row
            parsed = row["reading"]
            assert parsed["device_kind"] == "light", parsed
            assert parsed["availability"] == "available", parsed
            assert {key: parsed.get(key) for key in reading} == reading, parsed
            # A brightness or color change keeps the time the state last changed.
            assert near(parsed["last_changed"], state["last_changed"]), (parsed, state)
            assert "friendly_name" not in parsed, parsed
        assert rows[-1]["reading"].get("brightness") is None, rows[-1]

        selected = client.put(
            f"/api/selection/{identity}",
            json={"selected": True},
            headers={"X-Home-Keeper": "1"},
        )
        assert selected.status_code == 200, selected.text
        response = client.get(
            "/api/events", params={"entity_id": identity, "start": start.isoformat()}
        )
        assert response.status_code == 200, response.text
        # Newest first: off, the color change, the brightness change.
        off, color, brightness = response.json()["events"][:3]
        assert (off["old"]["state"], off["new"]["state"]) == ("on", "off")
        assert (color["old"].get("xy_color"), color["new"]["xy_color"]) == (None, [0.4, 0.3])
        assert (brightness["old"]["brightness"], brightness["new"]["brightness"]) == (100, 200)
        for change, state in zip((off, color, brightness), stored[:0:-1], strict=True):
            assert near(change["time"], state["last_updated"]), (change, state)


def main() -> None:
    started = time.monotonic()
    try:
        config = json.loads(compose("config", "--format", "json").stdout)
        published = {
            name: [port["host_ip"] for port in service.get("ports", [])]
            for name, service in config["services"].items()
            if service.get("ports")
        }
        assert published == {"api": ["127.0.0.1"], "homeassistant": ["127.0.0.1"]}, published
        services = config["services"]
        assert "home-assistant" not in services["api"]["networks"]
        # The controller reaches Home Assistant and, over an internal network, only the API.
        assert set(services["controller"]["networks"]) == {"control", "home-assistant"}
        assert "control" in services["api"]["networks"]
        assert "control" not in services["reader"]["networks"]
        assert "home-assistant" in services["reader"]["networks"]
        # The reader keeps nothing: no data volume, and no published port.
        assert not any(
            volume["target"] == "/data" for volume in services["reader"].get("volumes", [])
        )
        assert services["api"]["environment"]["HOME_KEEPER_READER_URL"] == "http://reader:8080"
        assert config["networks"]["control"].get("internal") is True
        mounts = {
            (name, volume["source"], volume.get("read_only", False))
            for name, service in config["services"].items()
            for volume in service.get("volumes", [])
            if volume["source"] in {"ha-token", "ha-owner", "ha-control-token"}
        }
        assert mounts == {
            ("ha-setup", "ha-token", False),
            ("ha-setup", "ha-owner", False),
            ("ha-setup", "ha-control-token", False),
            ("reader", "ha-token", True),
            ("controller", "ha-control-token", True),
        }, mounts
        compose("up", "--build", "--wait", "--wait-timeout", "400")
        states = {
            row["Service"]: row
            for row in map(json.loads, compose("ps", "-a", "--format", "json").stdout.splitlines())
        }
        assert states["ha-setup"]["ExitCode"] == 0
        with httpx.Client(base_url=KEEPER, timeout=5, trust_env=False) as client:
            wait_for_healthy_reader(client)
            catalog = client.get("/api/catalog").json()
            assert catalog["source_id"] == "home_assistant"
            assert catalog["home_assistant_url"] == HOME_ASSISTANT

            login = dict(
                line.split(": ", 1)
                for line in compose(
                    "run", "--rm", "--no-deps", "ha-setup", "login"
                ).stdout.splitlines()
            )
            assert login["Home Assistant"] == HOME_ASSISTANT
            assert log_in(login["Username"], login["Password"]) == "create_entry"
            token = compose("exec", "-T", "reader", "cat", "/ha-token/token").stdout.strip()
            assert token
            control = control_token()
            assert control
            assert control != token
            user = current_user(control)
            assert user["name"] == "Home Keeper control"
            assert user["is_admin"] is False
            assert user["is_owner"] is False
            wait_until_control_on(client)
            for endpoint in ("catalog", "health", "selection", "monitored", "control", "commands"):
                body = client.get(f"/api/{endpoint}").text
                assert login["Password"] not in body
                assert token not in body
                assert control not in body
            logs = compose("logs").stdout
            assert token not in logs
            assert control not in logs
            assert login["Password"] not in logs

        # The API must not reach Home Assistant; only the reader, the controller and setup share
        # its network.
        unreachable = compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "import socket; socket.create_connection(('homeassistant', 8123), timeout=3)",
            check=False,
        )
        assert unreachable.returncode != 0

        # The proof's template light loads with Home Assistant's restart below.
        compose(
            "exec",
            "-T",
            "homeassistant",
            "sh",
            "-c",
            "cat >> /config/configuration.yaml",
            stdin=PROOF_LIGHT,
        )

        # A restart removes HA's onboarding endpoint; setup must reuse both valid tokens.
        compose("stop", "reader", "homeassistant")
        compose("up", "--wait")
        assert "stored tokens are valid" in compose("logs", "ha-setup").stdout
        assert log_in(login["Username"], login["Password"]) == "create_entry"
        # Re-running setup with both tokens stored changes neither.
        assert control_token() == control
        assert compose("exec", "-T", "reader", "cat", "/ha-token/token").stdout.strip() == token

        prove_history_comes_from_the_recorder(token)

        # Losing the token cannot be repaired silently: setup fails and says how to reset.
        compose("run", "--rm", "--no-deps", "--entrypoint", "rm", "ha-setup", "/ha-token/token")
        failed = compose("up", "--wait", check=False)
        assert failed.returncode != 0
        assert "docker compose down --volumes" in compose("logs", "ha-setup").stdout
    finally:
        compose("down", "--volumes", check=False)
    print(f"Bundled Home Assistant smoke passed in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
