"""The device command routes: control heartbeat, how a refusal maps to HTTP, request
validation, the history scope, and collector ingestion while commands wait.

The command rules themselves are proved through `Commands` in test_commands.py.
"""

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from support.api import AUTH, CATALOG, NOW, TOKEN, Payload, event, observation, select
from support.control import DASHBOARD, DESK, ON, FakeController, beat, command

from home_keeper.api import create_app
from home_keeper.models import CommandRefused

LIGHT_OFF: Payload = {"state": "off"}


@dataclass
class House:
    client: TestClient
    clock: list[datetime]
    controller: FakeController
    revision: int

    def commands(self) -> list[dict[str, Any]]:
        response = self.client.get("/api/commands")
        assert response.status_code == 200, response.text
        records: list[dict[str, Any]] = response.json()["commands"]
        return records


@contextmanager
def home(tmp_path: Path, *, heartbeat: bool = True, wait: float = 0.3) -> Generator[House]:
    """bulb-a monitored and off; bulb-b discovered but not monitored."""
    clock = [NOW]
    controller = FakeController(clock)
    app = create_app(
        tmp_path / "commands.sqlite",
        TOKEN,
        now=lambda: clock[0],
        controller=controller,
        command_wait_seconds=wait,
        control_stale_seconds=15,
    )
    with TestClient(app) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH).raise_for_status()
        house = House(client, clock, controller, select(client))
        payload = observation(house.revision, source_entity_id=DESK, state="off")
        client.post("/internal/baselines", json=payload, headers=AUTH).raise_for_status()
        if heartbeat:
            beat(client, NOW)
        yield house
        controller.join()


@pytest.mark.parametrize(
    "body",
    [
        {"received_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"received_at": "2026-10-12T10:00:00"},
        {"received_at": "not a time"},
        {"received_at": NOW.isoformat(), "state": "on"},
        {},
    ],
)
def test_future_or_malformed_heartbeat_is_rejected_and_control_stays_off(
    tmp_path: Path, body: Payload
) -> None:
    with home(tmp_path, heartbeat=False) as house:
        response = house.client.post("/internal/control", json=body, headers=AUTH)
        assert response.status_code == 422, response.text
        assert house.client.get("/api/control").json()["state"] == "off"


def test_refused_command_is_a_conflict_with_its_code_and_message(tmp_path: Path) -> None:
    with home(tmp_path) as house:
        response = command(house.client, ON, entity="bulb-b")
        assert response.status_code == 409, response.text
        # The body the route declares in OpenAPI.
        refused = CommandRefused.model_validate_json(response.content)
        assert refused.detail.model_dump() == {
            "code": "not_monitored",
            "message": "Monitor this light before switching it",
        }
        assert house.controller.sent == []
        assert house.commands() == []


@pytest.mark.parametrize(
    ("entity", "status", "detail"),
    [("missing", 404, "Unknown entity"), ("room", 422, "Entity is unsupported")],
)
def test_unknown_or_unsupported_entity_is_not_found_or_invalid(
    tmp_path: Path, entity: str, status: int, detail: str
) -> None:
    with home(tmp_path) as house:
        response = command(house.client, ON, entity=entity)
        assert (response.status_code, response.json()) == (status, {"detail": detail})
        assert house.controller.sent == []
        assert house.commands() == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/devices/..%2Fselection%2Fbulb-a/command",
        "/api/devices/bulb-a%2F..%2Fbulb-b/command",
        "/api/devices//command",
    ],
)
def test_path_shaped_entity_ids_reach_no_light(tmp_path: Path, path: str) -> None:
    with home(tmp_path) as house:
        response = house.client.post(
            path,
            json={"command": ON, "sender": DASHBOARD},
            headers={"X-Home-Keeper": "1"},
        )
        assert response.status_code in (404, 405), response.text
        assert house.controller.sent == []
        assert house.commands() == []


def test_retired_light_command_route_is_not_found_and_reaches_nothing(tmp_path: Path) -> None:
    with home(tmp_path) as house:
        response = house.client.post(
            "/api/lights/bulb-a/command",
            json={"command": ON, "sender": DASHBOARD},
            headers={"X-Home-Keeper": "1"},
        )
        assert response.status_code == 404, response.text
        assert house.controller.sent == []
        assert house.commands() == []


@pytest.mark.parametrize(
    "body",
    [
        {"command": {"action": "toggle"}, "sender": DASHBOARD},
        {"command": {"action": "on", "brightness_pct": 0}, "sender": DASHBOARD},
        {"command": {"action": "on", "brightness_pct": 101}, "sender": DASHBOARD},
        {"command": {"action": "on", "brightness_pct": 30.5}, "sender": DASHBOARD},
        {"command": {"action": "off", "brightness_pct": 30}, "sender": DASHBOARD},
        {"command": {"action": "on", "service": "light.toggle"}, "sender": DASHBOARD},
        {"command": {"action": "on"}, "sender": DASHBOARD, "entity_id": "light.desk"},
        {"command": {"action": "on"}},
        {"sender": DASHBOARD},
        {"command": {"action": "on"}, "sender": {"kind": "agent"}},
        {"command": {"action": "on"}, "sender": {"kind": "dashboard", "name": "Mallory"}},
        {"command": {"action": "on"}, "sender": {"kind": "telegram", "name": "Ali"}},
        {"command": {"action": "on"}, "sender": {"kind": "telegram", "id": 1}},
        {"command": {"action": "on"}, "sender": {"kind": "telegram", "id": 1.5, "name": "A"}},
        {"command": {"action": "on"}, "sender": {"kind": "telegram", "id": 1, "name": ""}},
        {"command": {"action": "on"}, "sender": {"kind": "telegram", "id": 1, "name": "x" * 257}},
    ],
)
def test_malformed_command_is_an_invalid_request_and_reaches_nothing(
    tmp_path: Path, body: Payload
) -> None:
    with home(tmp_path) as house:
        response = house.client.post(
            "/api/devices/bulb-a/command", json=body, headers={"X-Home-Keeper": "1"}
        )
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": "Invalid request"}
        assert house.controller.sent == []
        assert house.commands() == []


def test_collector_ingestion_is_not_held_up_by_a_burst_of_command_waits(tmp_path: Path) -> None:
    burst = 40  # AnyIO's default worker-thread limit.
    with home(tmp_path, wait=3) as house:
        # Sends advance a private clock, so the heartbeat stays fresh for every command.
        house.controller.clock = [house.clock[0]]
        payload = event(
            house.revision,
            "during-burst",
            entity_id="bulb-a",
            source_entity_id=DESK,
            event_time=NOW.isoformat(),
            received_at=NOW.isoformat(),
            old=LIGHT_OFF,
            new={"state": "on", "brightness": 100},
        )
        statuses: list[int] = []
        latency: list[float] = []

        async def main() -> None:
            transport = httpx.ASGITransport(app=house.client.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as api:

                async def send_command() -> None:
                    response = await api.post(
                        "/api/devices/bulb-a/command",
                        json={"command": ON, "sender": DASHBOARD},
                        headers={"X-Home-Keeper": "1"},
                        timeout=30,
                    )
                    statuses.append(response.status_code)

                async with anyio.create_task_group() as group:
                    for _ in range(burst):
                        group.start_soon(send_command)
                    await anyio.sleep(0.5)
                    started = time.monotonic()
                    response = await api.post(
                        "/internal/events", json=payload, headers=AUTH, timeout=30
                    )
                    latency.append(time.monotonic() - started)
                    assert response.json()["recorded"], response.text

        anyio.run(main)
    assert latency[0] < 1.0, f"event ingestion waited {latency[0]:.2f}s behind command waits"
    assert len(statuses) == burst
    assert all(status < 500 for status in statuses), statuses


def test_collector_ingestion_is_not_held_up_by_stalled_controller_sends(tmp_path: Path) -> None:
    burst = 40  # AnyIO's default worker-thread limit.
    with home(tmp_path, wait=0.1) as house:
        house.controller.clock = [house.clock[0]]
        # A stalled controller: every send blocks until the collector's event is recorded.
        release = threading.Event()
        house.controller.on_send = lambda: release.wait(10)
        payload = event(
            house.revision,
            "during-stall",
            entity_id="bulb-a",
            source_entity_id=DESK,
            event_time=NOW.isoformat(),
            received_at=NOW.isoformat(),
            old=LIGHT_OFF,
            new={"state": "on", "brightness": 100},
        )
        statuses: list[int] = []
        latency: list[float] = []

        async def main() -> None:
            transport = httpx.ASGITransport(app=house.client.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as api:

                async def send_command() -> None:
                    response = await api.post(
                        "/api/devices/bulb-a/command",
                        json={"command": ON, "sender": DASHBOARD},
                        headers={"X-Home-Keeper": "1"},
                        timeout=30,
                    )
                    statuses.append(response.status_code)

                async with anyio.create_task_group() as group:
                    for _ in range(burst):
                        group.start_soon(send_command)
                    # Let every send start that the API admits; without a limit, all 40 do.
                    deadline = time.monotonic() + 5
                    while len(house.controller.sent) < burst and time.monotonic() < deadline:  # noqa: ASYNC110 -- worker threads fill `sent`, so no event fires
                        await anyio.sleep(0.05)
                    started = time.monotonic()
                    try:
                        response = await api.post(
                            "/internal/events", json=payload, headers=AUTH, timeout=30
                        )
                    finally:
                        latency.append(time.monotonic() - started)
                        release.set()
                    assert response.json()["recorded"], response.text

        anyio.run(main)
    assert latency[0] < 1.0, f"event ingestion waited {latency[0]:.2f}s behind controller sends"
    assert len(statuses) == burst
    assert all(status < 500 for status in statuses), statuses


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 501},
        {"cursor": "not-a-cursor"},
        # Year 1 with a positive offset falls before the first UTC instant.
        {"cursor": "0001-01-01T00:00:00+14:00|light.desk"},
        {"start": "2026-10-12T10:00:00"},
        {"start": NOW.isoformat(), "end": (NOW - timedelta(seconds=1)).isoformat()},
    ],
)
def test_invalid_command_scope_is_rejected(tmp_path: Path, params: dict[str, str | int]) -> None:
    with home(tmp_path) as house:
        response = house.client.get("/api/commands", params=params)
        assert response.status_code == 422, response.text
