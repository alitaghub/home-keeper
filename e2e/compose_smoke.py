"""Bounded external smoke test for a running synthetic Compose deployment.

Run after `docker compose up --build -d`; it uses no internal credentials.
"""

import argparse
import json
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
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
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
            else {"Content-Type": "application/json", "X-Home-Observer": "1", "Origin": base}
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
            subprocess.check_output(["docker", "compose", "config", "--format", "json"], timeout=10)
        )
        published: list[str] = []
        for name, service in config["services"].items():
            for port in service.get("ports", []):
                assert port.get("host_ip") == "127.0.0.1", f"Non-local published port on {name}"
                published.append(name)
        assert published == ["api"], f"Unexpected published services: {published}"
        assert not config["services"]["collector"].get("ports"), "Collector port published"

    def ready() -> bool:
        catalog = get("/api/catalog")["entities"]
        return (
            {entry["id"] for entry in catalog} == EXPECTED
            and sum(entry["supported"] for entry in catalog) == 2
            and get("/api/health")["status"] == "healthy"
        )

    wait_until("five-entity catalog and healthy collector", ready)
    assert "<html" in request("/").lower(), "Dashboard HTML not served"
    assert "fetch(" in request("/app.js"), "Dashboard JavaScript not served"
    entity = "light.hue_desk"
    initially_selected = entity in get("/api/selection")["entity_ids"]
    try:
        # Require confirmation for this selection, rather than a cached fresh baseline.
        request(f"/api/selection/{entity}", {"selected": False})
        selection = json.loads(request(f"/api/selection/{entity}", {"selected": True}))
        assert entity in selection["entity_ids"]

        def confirmed() -> Any:
            for entry in get("/api/monitored")["entities"]:
                if entry["id"] == entity and entry["freshness"] == "fresh" and entry["baseline"]:
                    return entry["baseline"]
            return None

        baseline = wait_until("selected fresh attributed baseline from collector", confirmed)
        assert baseline["entity_id"] == entity
        assert baseline["source_id"] == "demo"
        assert baseline["state"] in ("on", "off", "unavailable")
        assert baseline["availability"] == (
            "unavailable" if baseline["state"] == "unavailable" else "available"
        )
        for field in ("received_at", "persisted_at", "last_changed"):
            offset = datetime.fromisoformat(baseline[field]).utcoffset()
            assert offset is not None
            assert offset.total_seconds() == 0
        assert baseline["received_at"] != baseline["last_changed"], (
            "Receipt confused with source change"
        )
        json.loads(request(f"/api/selection/{entity}", {"selected": False}))
        assert entity not in {entry["id"] for entry in get("/api/monitored")["entities"]}
        history = get(f"/api/baselines?entity_id={entity}")["baselines"]
        events = get(f"/api/events?entity_id={entity}")["events"]
        assert baseline["id"] in {record["id"] for record in history + events}, (
            "Removed evidence lost"
        )
    finally:
        request(f"/api/selection/{entity}", {"selected": initially_selected})
    print(
        "Compose smoke passed: local bindings, healthy collector, five discoveries, "
        "dashboard assets, selected fresh baseline, removal and retained history"
    )


if __name__ == "__main__":
    main()
