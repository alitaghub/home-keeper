"""Boot the default stack with the real pinned Home Assistant image and prove setup needs no human.

Runs with the `control` profile, so setup also mints the controller's own token.

Uses its own Compose project and ports, and deletes its volumes at the end.
"""

import json
import os
import subprocess
import time
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


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
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

        # A restart removes HA's onboarding endpoint; setup must reuse both valid tokens.
        compose("stop", "reader", "homeassistant")
        compose("up", "--wait")
        assert "stored tokens are valid" in compose("logs", "ha-setup").stdout
        assert log_in(login["Username"], login["Password"]) == "create_entry"
        # Re-running setup with both tokens stored changes neither.
        assert control_token() == control
        assert compose("exec", "-T", "reader", "cat", "/ha-token/token").stdout.strip() == token

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
