"""The device command routes: control heartbeat, how a refusal maps to HTTP, request
validation, the history scope, dashboard reads while commands wait, and what the API does
to the command log at startup and on requests.

The command rules themselves are proved through `Commands` in test_commands.py.
"""

import json
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import anyio
import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from support.api import AUTH, BROWSER, NOW, TOKEN, Payload, catalog_reader
from support.control import DASHBOARD, DESK, ON, FakeController, beat, command
from support.reader import FakeReader

from home_keeper.api import create_app
from home_keeper.models import CommandRefused
from home_keeper.store import Store


@dataclass
class House:
    client: TestClient
    clock: list[datetime]
    controller: FakeController
    reader: FakeReader

    def commands(self) -> list[dict[str, Any]]:
        response = self.client.get("/api/commands")
        assert response.status_code == 200, response.text
        records: list[dict[str, Any]] = response.json()["commands"]
        return records


@contextmanager
def home(database: str, *, heartbeat: bool = True, wait: float = 0.3) -> Generator[House]:
    """bulb-a monitored and off in Home Assistant; bulb-b in the catalog but not monitored.

    The API's clock runs with real time from NOW, so a command's wait for the device's new
    state ends; sends move `clock` on, as the controller's own clock.
    """
    clock = [NOW]
    started = time.monotonic()
    controller = FakeController(clock)
    reader = catalog_reader()
    reader.set("bulb-a", DESK, {"state": "off"})
    app = create_app(
        database,
        TOKEN,
        now=lambda: clock[0] + timedelta(seconds=time.monotonic() - started),
        reader=reader,
        controller=controller,
        command_wait_seconds=wait,
        control_stale_seconds=15,
    )
    with TestClient(app) as client:
        client.put(
            "/api/selection/bulb-a", json={"selected": True}, headers=BROWSER
        ).raise_for_status()
        house = House(client, clock, controller, reader)
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
    database: str, body: Payload
) -> None:
    with home(database, heartbeat=False) as house:
        response = house.client.post("/internal/control", json=body, headers=AUTH)
        assert response.status_code == 422, response.text
        assert house.client.get("/api/control").json()["state"] == "off"


def test_refused_command_is_a_conflict_with_its_code_and_message(database: str) -> None:
    with home(database) as house:
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
    database: str, entity: str, status: int, detail: str
) -> None:
    with home(database) as house:
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
def test_path_shaped_entity_ids_reach_no_light(database: str, path: str) -> None:
    with home(database) as house:
        response = house.client.post(
            path,
            json={"command": ON, "sender": DASHBOARD},
            headers={"X-Home-Keeper": "1"},
        )
        assert response.status_code in (404, 405), response.text
        assert house.controller.sent == []
        assert house.commands() == []


def test_retired_light_command_route_is_not_found_and_reaches_nothing(database: str) -> None:
    with home(database) as house:
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
        # Postgres JSONB cannot hold a NUL character, so it never reaches the command log.
        {"command": {"action": "on"}, "sender": {"kind": "telegram", "id": 1, "name": "A\u0000B"}},
    ],
)
def test_malformed_command_is_an_invalid_request_and_reaches_nothing(
    database: str, body: Payload
) -> None:
    with home(database) as house:
        response = house.client.post(
            "/api/devices/bulb-a/command", json=body, headers={"X-Home-Keeper": "1"}
        )
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": "Invalid request"}
        assert house.controller.sent == []
        assert house.commands() == []


def dashboard_read_latency(
    house: House,
    burst: int,
    *,
    until_sent: bool,
    after_read: Callable[[], object] = lambda: None,
) -> list[int]:
    """Send `burst` commands at once, then time one /api/monitored read among them.

    `after_read` runs once the read is answered. Returns the command statuses; fails when
    the read waited behind the commands.
    """
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
                if until_sent:
                    # Let every send start that the API admits; without a limit, all of them do.
                    deadline = time.monotonic() + 5
                    while len(house.controller.sent) < burst and time.monotonic() < deadline:  # noqa: ASYNC110 -- worker threads fill `sent`, so no event fires
                        await anyio.sleep(0.05)
                else:
                    await anyio.sleep(0.5)
                started = time.monotonic()
                try:
                    response = await api.get("/api/monitored", timeout=30)
                finally:
                    latency.append(time.monotonic() - started)
                    after_read()
                assert response.status_code == 200, response.text

    anyio.run(main)
    assert latency[0] < 1.0, f"the dashboard read waited {latency[0]:.2f}s behind commands"
    return statuses


def test_dashboard_reads_are_not_held_up_by_a_burst_of_command_waits(database: str) -> None:
    burst = 40  # AnyIO's default worker-thread limit.
    with home(database, wait=3) as house:
        # Sends advance a private clock, so the heartbeat stays fresh for every command.
        house.controller.clock = [house.clock[0]]
        statuses = dashboard_read_latency(house, burst, until_sent=False)
    assert len(statuses) == burst
    assert all(status < 500 for status in statuses), statuses


def test_dashboard_reads_are_not_held_up_by_stalled_controller_sends(database: str) -> None:
    burst = 40  # AnyIO's default worker-thread limit.
    with home(database, wait=0.1) as house:
        house.controller.clock = [house.clock[0]]
        # A stalled controller: every send blocks until the dashboard read is answered.
        release = threading.Event()
        house.controller.on_send = lambda: release.wait(10)
        try:
            statuses = dashboard_read_latency(house, burst, until_sent=True, after_read=release.set)
        finally:
            release.set()
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
def test_invalid_command_scope_is_rejected(database: str, params: dict[str, str | int]) -> None:
    with home(database) as house:
        response = house.client.get("/api/commands", params=params)
        assert response.status_code == 422, response.text


def seed_commands(database: str, *records: Payload) -> None:
    """Store command records before the API starts, as an earlier run left them."""
    store = Store(database)
    store.open()
    try:
        with store.transaction() as db:
            for record in records:
                db.execute(
                    "INSERT INTO commands VALUES(%s, %s, %s, %s)",
                    (record["id"], record["entity_id"], record["sent_at"], json.dumps(record)),
                )
    finally:
        store.close()


def stored_record(identity: str, sent_at: datetime, outcome: str) -> Payload:
    return {
        "id": identity,
        "entity_id": "bulb-a",
        "device_kind": "light",
        "command": ON,
        "sender": DASHBOARD,
        "sent_at": sent_at.isoformat(),
        "outcome": outcome,
    }


def test_a_command_left_pending_by_a_crash_shows_as_interrupted_after_start(
    database: str,
) -> None:
    seed_commands(database, stored_record("left", NOW - timedelta(minutes=1), "pending"))
    with home(database) as house:
        [left] = house.commands()
        assert (left["id"], left["outcome"]) == ("left", "interrupted")


def test_the_api_deletes_commands_older_than_retention(database: str) -> None:
    seed_commands(
        database,
        stored_record("expired", NOW - timedelta(days=31), "observed"),
        stored_record("recent", NOW - timedelta(days=1), "observed"),
    )
    with home(database):
        pass
    with psycopg.connect(database) as db:
        rows = db.execute("SELECT id FROM commands ORDER BY id").fetchall()
    assert rows == [("recent",)]
