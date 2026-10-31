"""Run the agent-panel Playwright spec on its own demo stack, where Aegra serves the real
agent graph with the deterministic model of dashboard_graph.py. Deletes its volumes at the end.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
PROJECT = "keeper-agent-panel-proof"
HOME_KEEPER_PORT = 18006
AGENT_PORT = 18228
# The browser address, as a user opens it.
DASHBOARD = f"http://localhost:{HOME_KEEPER_PORT}"
TIMEOUT = 90
COMPOSE_ARGS = [
    "-p",
    PROJECT,
    "-f",
    str(REPO / "compose.demo.yaml"),
    "-f",
    str(REPO / "e2e/compose.agent-panel.yaml"),
    "--profile",
    "agent",
    "--profile",
    "control",
]
ENV = os.environ | {
    "HOME_KEEPER_PORT": str(HOME_KEEPER_PORT),
    "AGENT_PORT": str(AGENT_PORT),
    # Placeholders with a valid shape; the deterministic model never calls a provider.
    "AZURE_OPENAI_ENDPOINT": "https://placeholder.invalid",
    "AZURE_OPENAI_API_KEY": "placeholder",
}


def compose(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", "compose", *COMPOSE_ARGS, *args],
        cwd=REPO,
        env=ENV,
        check=check,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return result.stdout + result.stderr


def wait_for_agent_through_dashboard() -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{DASHBOARD}/agent/info", timeout=3, trust_env=False).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError("Aegra did not answer through the dashboard's /agent")


def main() -> None:
    started = time.monotonic()
    try:
        compose("up", "--build", "--wait")
        wait_for_agent_through_dashboard()
        subprocess.run(
            ["pnpm", "--filter", "@home-keeper/web", "e2e", "--project", "agent"],
            cwd=REPO,
            env=os.environ
            | {
                "E2E_BASE_URL": DASHBOARD,
                # Lets the spec stop the controller mid-approval.
                "E2E_COMPOSE_ARGS": json.dumps(COMPOSE_ARGS),
            },
            check=True,
            timeout=900,
        )
        print(
            json.dumps(
                {
                    "agent_panel_compose": "passed",
                    "seconds": round(time.monotonic() - started, 2),
                    "proved": "dashboard /agent reaches Aegra, panel layout and lazy load, "
                    "streamed answer once, reload, approve, reject, unreachable controller",
                }
            )
        )
    except BaseException:
        (REPO / "agent-panel-compose-logs.txt").write_text(
            compose("logs", "--no-color", check=False)
        )
        raise
    finally:
        compose("down", "--volumes", check=False)


if __name__ == "__main__":
    main()
