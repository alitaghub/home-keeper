"""Boot the default stack with the real pinned Home Assistant image and prove setup needs no human.

Uses its own Compose project and ports, and deletes its volumes at the end.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
PROJECT = "observer-home-proof"
OBSERVER_PORT = "18002"
HA_PORT = "18124"
OBSERVER = f"http://127.0.0.1:{OBSERVER_PORT}"
HOME_ASSISTANT = f"http://127.0.0.1:{HA_PORT}"


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(REPO / "compose.yaml"), *args],
        env=os.environ | {"OBSERVER_PORT": OBSERVER_PORT, "HA_PORT": HA_PORT},
        cwd=REPO,
        check=check,
        capture_output=True,
        text=True,
        timeout=600,
    )


def wait_for_healthy_collector(client: httpx.Client) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if client.get("/api/health").json()["status"] == "healthy":
            return
        time.sleep(1)
    raise AssertionError("Collector never reported healthy against the bundled Home Assistant")


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
        assert "home-assistant" not in config["services"]["api"]["networks"]
        mounts = {
            (name, volume["source"], volume.get("read_only", False))
            for name, service in config["services"].items()
            for volume in service.get("volumes", [])
            if volume["source"] in {"ha-token", "ha-owner"}
        }
        assert mounts == {
            ("ha-setup", "ha-token", False),
            ("ha-setup", "ha-owner", False),
            ("collector", "ha-token", True),
        }, mounts
        compose("up", "--build", "--wait", "--wait-timeout", "400")
        states = {
            row["Service"]: row
            for row in map(json.loads, compose("ps", "-a", "--format", "json").stdout.splitlines())
        }
        assert states["ha-setup"]["ExitCode"] == 0
        with httpx.Client(base_url=OBSERVER, timeout=5, trust_env=False) as client:
            wait_for_healthy_collector(client)
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
            token = compose("exec", "-T", "collector", "cat", "/ha-token/token").stdout.strip()
            assert token
            for endpoint in ("catalog", "health", "selection", "monitored"):
                body = client.get(f"/api/{endpoint}").text
                assert login["Password"] not in body
                assert token not in body
            assert token not in compose("logs").stdout

        # The API must not reach Home Assistant; only the collector and setup share its network.
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

        # A second start keeps the stored token and the owner login.
        compose("up", "--wait")
        assert "stored token is valid" in compose("logs", "ha-setup").stdout
        assert log_in(login["Username"], login["Password"]) == "create_entry"

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
