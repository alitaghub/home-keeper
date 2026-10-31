"""Bounded smoke test for the `agent` Compose profile on a running demo stack.

Run after `docker compose -f compose.demo.yaml --profile agent up --build --wait` with placeholder
AZURE_OPENAI_* values. It never calls the model.
"""

import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
# The browser addresses: localhost, which also reaches the stack through an SSH port forward.
AGENT_URL = f"http://localhost:{os.environ.get('AGENT_PORT', '2026')}"
DASHBOARD_URL = f"http://localhost:{os.environ.get('HOME_KEEPER_PORT', '8000')}"
AGENT_SERVICES = {"agent"}
TIMEOUT = 90


def compose(*args: str, file: str = "compose.demo.yaml") -> str:
    # COMPOSE_PROJECT_NAME, when the caller sets it, selects the same project here.
    return subprocess.run(
        ["docker", "compose", "-f", str(REPO / file), *args],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    ).stdout


def config(file: str, *profile: str) -> dict[str, Any]:
    return json.loads(compose(*profile, "config", "--format", "json", file=file))


def check_profile_config(file: str) -> None:
    plain = config(file)
    assert not AGENT_SERVICES & plain["services"].keys(), f"{file}: agent services without profile"
    # The one Postgres runs in every stack: the keeper keeps its data there too.
    assert "postgres" in plain["services"], f"{file}: no Postgres without the agent profile"
    services: dict[str, Any] = config(file, "--profile", "agent")["services"]
    assert services.keys() >= AGENT_SERVICES, f"{file}: missing {AGENT_SERVICES - services.keys()}"
    # The browser reaches Aegra only through the dashboard, so no chat service runs.
    assert "chat" not in services, f"{file}: the agent profile still starts a chat service"
    assert not [
        name for name, service in services.items() if "redis" in f"{name} {service.get('image')}"
    ], f"{file}: Redis in the agent profile"
    agent = services["agent"]
    assert "home-assistant" not in agent.get("networks", {}), f"{file}: agent joins home-assistant"
    # Only its own database password: no keeper data, secrets or other passwords.
    mounted = {volume.get("source") for volume in agent.get("volumes", [])}
    assert mounted == {"agent-db-password"}, f"{file}: agent mounts {mounted}"
    for name, service in services.items():
        for port in service.get("ports", []):
            assert port.get("host_ip") == "127.0.0.1", f"{file}: non-local port on {name}"


def wait_until[T](description: str, predicate: Callable[[], T]) -> T:
    deadline = time.monotonic() + TIMEOUT
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except httpx.HTTPError as error:
            last_error = type(error).__name__
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {description}; last transport error={last_error}")


def agent_healthy() -> bool:
    response = httpx.get(f"{AGENT_URL}/health", timeout=3)
    return response.status_code == 200 and response.json()["status"] == "healthy"


def main() -> None:
    started = time.monotonic()
    for file in ("compose.yaml", "compose.demo.yaml"):
        check_profile_config(file)

    wait_until("healthy Aegra", agent_healthy)
    assistants = httpx.post(
        f"{AGENT_URL}/assistants/search", json={"graph_id": "agent"}, timeout=10
    ).raise_for_status()
    assert "agent" in [assistant["graph_id"] for assistant in assistants.json()], assistants.text

    # The dashboard reaches Aegra through its own address, so no website needs CORS.
    def allowed_origin(origin: str) -> str | None:
        preflight = httpx.options(
            f"{AGENT_URL}/threads/search",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
            timeout=3,
        )
        return preflight.headers.get("access-control-allow-origin")

    for origin in ("http://evil.example.test", DASHBOARD_URL, "http://localhost:3000"):
        assert allowed_origin(origin) is None, f"Aegra allows the origin {origin}"

    # The dashboard forwards /agent/* to Aegra with the prefix stripped.
    info = httpx.get(f"{DASHBOARD_URL}/agent/info", timeout=10).raise_for_status()
    assert info.json()["name"] == "Aegra", info.text
    forwarded = httpx.post(
        f"{DASHBOARD_URL}/agent/assistants/search", json={"graph_id": "agent"}, timeout=10
    ).raise_for_status()
    assert "agent" in [assistant["graph_id"] for assistant in forwarded.json()], forwarded.text
    # A form post another site starts needs no preflight; Aegra reads its text/plain as JSON.
    for site in ("cross-site", "same-site"):
        refused = httpx.post(
            f"{DASHBOARD_URL}/agent/threads",
            content=json.dumps({"metadata": {"source": "dashboard"}}),
            headers={
                "Content-Type": "text/plain",
                "Sec-Fetch-Site": site,
                "Origin": "http://evil.example.test",
            },
            timeout=10,
        )
        assert refused.status_code == 403, f"A {site} /agent request answered {refused}"

    # Threads live in Postgres, so a thread outlives an agent restart. No run, so no model call.
    marker = str(uuid.uuid4())
    thread = (
        httpx.post(f"{AGENT_URL}/threads", json={"metadata": {"smoke": marker}}, timeout=10)
        .raise_for_status()
        .json()
    )
    compose("--profile", "agent", "restart", "agent")
    wait_until("healthy Aegra after restart", agent_healthy)
    kept = httpx.get(f"{AGENT_URL}/threads/{thread['thread_id']}", timeout=10).raise_for_status()
    assert kept.json()["metadata"]["smoke"] == marker, kept.text

    print(
        json.dumps(
            {
                "agent_compose": "passed",
                "seconds": round(time.monotonic() - started, 2),
                "proved": "agent profile config without chat, Aegra health, graph listed, "
                "no CORS origin, dashboard /agent reaches Aegra, "
                "cross-site and same-site /agent refused, "
                "thread kept across restart",
            }
        )
    )


if __name__ == "__main__":
    main()
