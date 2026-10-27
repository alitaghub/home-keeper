"""Bounded smoke test for the `agent` Compose profile on a running demo stack.

Run after `docker compose -f compose.demo.yaml --profile agent up --build --wait` with placeholder
AZURE_OPENAI_* values. It never calls the model.
"""

import json
import os
import re
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
CHAT_URL = f"http://localhost:{os.environ.get('CHAT_PORT', '3000')}"
AGENT_SERVICES = {"postgres", "agent", "chat"}
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
    services: dict[str, Any] = config(file, "--profile", "agent")["services"]
    assert services.keys() >= AGENT_SERVICES, f"{file}: missing {AGENT_SERVICES - services.keys()}"
    assert not [
        name for name, service in services.items() if "redis" in f"{name} {service.get('image')}"
    ], f"{file}: Redis in the agent profile"
    agent = services["agent"]
    assert "home-assistant" not in agent.get("networks", {}), f"{file}: agent joins home-assistant"
    mounted = {volume.get("source") for volume in agent.get("volumes", [])}
    assert not mounted & set(plain.get("volumes", {})), f"{file}: agent mounts {mounted}"
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

    # The browser calls Aegra directly, so Aegra allows exactly the chat origin and no other.
    def allowed_origin(origin: str) -> str | None:
        preflight = httpx.options(
            f"{AGENT_URL}/threads/search",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
            timeout=3,
        )
        return preflight.headers.get("access-control-allow-origin")

    assert allowed_origin("http://evil.example.test") is None, "Aegra allows any origin"
    assert allowed_origin(CHAT_URL) == CHAT_URL, "Aegra does not allow exactly the chat origin"

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

    def chat_page() -> str:
        response = httpx.get(CHAT_URL, timeout=5)
        return response.text if response.status_code == 200 else ""

    page = wait_until("chat page", chat_page)
    assert "<html" in page.lower(), "Chat page HTML not served"
    # Next.js inlines the Aegra URL and assistant id into the page's scripts at build time.
    scripts = re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', page)
    assert scripts, "Chat page references no scripts"
    bundle = "".join(httpx.get(CHAT_URL + src, timeout=10).text for src in scripts)
    assert re.search(rf'"{re.escape(AGENT_URL)}"[^"]{{0,20}}"agent"', bundle), (
        f"Chat is not built for {AGENT_URL} and assistant 'agent'"
    )
    # The chat server must not proxy Aegra: its proxy answers any origin with `*`.
    proxied = httpx.get(f"{CHAT_URL}/api/info", timeout=10)
    assert not (
        proxied.status_code == 200
        and proxied.headers.get("content-type", "").startswith("application/json")
        and {"version", "flags"} <= proxied.json().keys()
    ), "Chat server proxies Aegra"

    print(
        json.dumps(
            {
                "agent_compose": "passed",
                "seconds": round(time.monotonic() - started, 2),
                "proved": "agent profile config, Aegra health, graph listed, chat-only CORS, "
                "thread kept across restart, chat page built for this agent, no chat proxy",
            }
        )
    )


if __name__ == "__main__":
    main()
