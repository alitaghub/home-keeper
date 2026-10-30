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
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

EXPECTED = {
    "light.hue_desk",
    "light.hue_reading",
    "light.hue_room",
    "switch.smart_plug",
    "sensor.ungrouped",
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

    def request(path: str, body: dict[str, object] | None = None) -> str:
        data = None if body is None else json.dumps(body).encode()
        headers = (
            {}
            if body is None
            else {"Content-Type": "application/json", "X-Home-Keeper": "1", "Origin": base}
        )
        with urlopen(
            Request(
                base + path, data=data, headers=headers, method="GET" if body is None else "PUT"
            ),
            timeout=3,
        ) as response:
            return response.read().decode()

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
        assert published == ["api"], f"Unexpected published services: {published}"
        assert not config["services"]["reader"].get("ports"), "Reader port published"
        assert "controller" in config["services"], "Control profile adds no controller"

    def ready() -> bool:
        catalog = get("/api/catalog")["entities"]
        return (
            {entry["id"] for entry in catalog} == EXPECTED
            # Two single lights and the smart plug; the room group and the sensor are not.
            and sum(entry["supported"] for entry in catalog) == 3
            and get("/api/health")["status"] == "healthy"
        )

    wait_until("five-entity catalog and healthy reader", ready)
    page = request("/")
    assert "<html" in page.lower(), "Dashboard HTML not served"
    script = re.search(r'<script[^>]+src="(/assets/[^"]+\.js)"', page)
    assert script, "Dashboard script not referenced"
    assert "/api/" in request(script.group(1)), "Dashboard JavaScript not served"
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
        json.loads(request(f"/api/selection/{entity}", {"selected": False}))
        assert entity not in {entry["id"] for entry in get("/api/monitored")["entities"]}
    finally:
        request(f"/api/selection/{entity}", {"selected": initially_selected})
    print(
        "Compose smoke passed: local bindings, healthy reader, five discoveries, "
        "dashboard assets, the selected light's current state and its removal"
    )


if __name__ == "__main__":
    main()
