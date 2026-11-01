"""Bounded external smoke test for a running synthetic Compose deployment.

Run after `docker compose -f compose.demo.yaml up --build -d`; it uses no internal credentials.
"""

import argparse
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Only the dashboard's own scripts and styles run, and no other site can frame it.
DASHBOARD_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)

EXPECTED = {
    "light.hue_desk",
    "light.hue_reading",
    "light.hue_room",
    "switch.smart_plug",
    "cover.garage_door",
    "fan.ceiling_fan",
    "binary_sensor.front_door",
    "sensor.ungrouped",
}
# The demo home's kinds beyond lights: the garage door is a generic `state` device, the fan and
# the door sensor are on/off devices.
OTHER_KINDS = {
    "cover.garage_door": "state",
    "fan.ceiling_fan": "on_off",
    "binary_sensor.front_door": "on_off",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=f"http://127.0.0.1:{os.environ.get('HOME_KEEPER_PORT', '8000')}"
    )
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--skip-bindings", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    base = args.base_url.rstrip("/")

    def fetch(
        path: str, body: dict[str, object] | None = None, origin: str = base
    ) -> tuple[str, Message]:
        data = None if body is None else json.dumps(body).encode()
        headers = (
            {}
            if body is None
            else {"Content-Type": "application/json", "X-Home-Keeper": "1", "Origin": origin}
        )
        with urlopen(
            Request(
                base + path, data=data, headers=headers, method="GET" if body is None else "PUT"
            ),
            timeout=3,
        ) as response:
            return response.read().decode(), response.headers

    def request(path: str, body: dict[str, object] | None = None) -> str:
        return fetch(path, body)[0]

    def get(path: str) -> Any:
        return json.loads(request(path))

    def wait_until[T](description: str, predicate: Callable[[], T]) -> T:
        deadline = time.monotonic() + args.timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = type(error).__name__
            time.sleep(0.25)
        raise AssertionError(
            f"Timed out waiting for {description}; last transport error={last_error}"
        )

    if not args.skip_bindings:
        config = json.loads(
            subprocess.check_output(
                # The control profile adds the controller, which must publish nothing.
                [
                    "docker",
                    "compose",
                    "-f",
                    "compose.demo.yaml",
                    "--profile",
                    "control",
                    "config",
                    "--format",
                    "json",
                ],
                timeout=10,
            )
        )
        published: list[str] = []
        for name, service in config["services"].items():
            for port in service.get("ports", []):
                assert port.get("host_ip") == "127.0.0.1", f"Non-local published port on {name}"
                published.append(name)
        # The web container is the only browser entry point; the API is internal.
        assert published == ["web"], f"Unexpected published services: {published}"
        assert not config["services"]["api"].get("ports"), "API port published"
        assert not config["services"]["reader"].get("ports"), "Reader port published"
        assert "controller" in config["services"], "Control profile adds no controller"

    def ready() -> bool:
        catalog = get("/api/catalog")["entities"]
        return (
            {entry["id"] for entry in catalog} == EXPECTED
            # Two single lights, the smart plug, the garage door (a generic `state` device), the
            # ceiling fan and the front door sensor (on/off devices); the room group and the
            # sensor, which has no device, are not.
            and sum(entry["supported"] for entry in catalog) == 6
            and {
                entry["id"]: entry["device_kind"] for entry in catalog if entry["id"] in OTHER_KINDS
            }
            == OTHER_KINDS
            and get("/api/health")["status"] == "healthy"
        )

    wait_until("eight-entity catalog and healthy reader", ready)
    page, page_headers = fetch("/")
    assert "<html" in page.lower(), "Dashboard HTML not served"
    script = re.search(r'<script[^>]+src="(/assets/[^"]+\.js)"', page)
    assert script, "Dashboard script not referenced"
    bundle, bundle_headers = fetch(script.group(1))
    assert "/api/" in bundle, "Dashboard JavaScript not served"
    for name, headers in (("HTML", page_headers), ("JavaScript", bundle_headers)):
        assert headers["Content-Security-Policy"] == DASHBOARD_CSP, f"Dashboard {name} CSP"
        assert headers["X-Content-Type-Options"] == "nosniff", f"Dashboard {name} nosniff"
        assert headers["Cache-Control"] == "no-store", f"Dashboard {name} cached"
    if not args.skip_bindings:
        # `web` serves the dashboard, so the API image must not carry a copy.
        subprocess.run(
            [
                *("docker", "compose", "-f", "compose.demo.yaml", "exec", "-T", "api"),
                *("python", "-c", "import pathlib; assert not pathlib.Path('web').exists()"),
            ],
            check=True,
            timeout=30,
        )
    entity = "light.hue_desk"
    initially_selected = entity in get("/api/selection")["entity_ids"]
    try:
        request(f"/api/selection/{entity}", {"selected": False})
        selection = json.loads(request(f"/api/selection/{entity}", {"selected": True}))
        assert entity in selection["entity_ids"]

        def current() -> Any:
            for entry in get("/api/monitored")["entities"]:
                if entry["id"] == entity and entry["state"]:
                    return entry
            return None

        # The state the demo home reports now, read through the reader on this request.
        monitored = wait_until("the selected light's current state", current)
        assert monitored["missing"] is False
        state = monitored["state"]
        assert state["state"] in ("on", "off", "unavailable")
        assert state["availability"] == (
            "unavailable" if state["state"] == "unavailable" else "available"
        )
        offset = datetime.fromisoformat(state["last_changed"]).utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 0
        try:
            fetch(f"/api/selection/{entity}", {"selected": False}, origin="http://evil.example")
            refused = None
        except HTTPError as error:
            refused = error.code
        assert refused == 403, f"Foreign-origin write through the dashboard answered {refused}"
        assert entity in get("/api/selection")["entity_ids"], "Foreign-origin write applied"
        # `web` must pass the browser's Host through: the API refuses unknown hosts,
        # which stops DNS rebinding.
        try:
            urlopen(Request(base + "/api/selection", headers={"Host": "evil.example"}), timeout=3)
            foreign_host = None
        except HTTPError as error:
            foreign_host = error.code
        assert foreign_host == 400, f"Foreign Host through the dashboard answered {foreign_host}"
        json.loads(request(f"/api/selection/{entity}", {"selected": False}))
        assert entity not in {entry["id"] for entry in get("/api/monitored")["entities"]}
        # Without the agent profile, /agent fails instead of serving the dashboard page,
        # so the agent panel can say the agent isn't running.
        try:
            urlopen(Request(base + "/agent/info"), timeout=10)
            without_agent = None
        except HTTPError as error:
            without_agent = error.code
        assert without_agent == 502, f"/agent without the agent answered {without_agent}"
    finally:
        request(f"/api/selection/{entity}", {"selected": initially_selected})
    print(
        "Compose smoke passed: only the web container published, healthy reader, five "
        "discoveries, dashboard assets with security headers, no dashboard in the API "
        "image, foreign-origin write refused, the selected light's current state and its removal, "
        "/agent failing without the agent"
    )


if __name__ == "__main__":
    main()
