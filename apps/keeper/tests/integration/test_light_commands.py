"""Light commands: control capability, refusals, observed outcomes and the command audit."""

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from support.api import AUTH, CATALOG, NOW, TOKEN, Payload, event, observation, select
from support.control import (
    BROWSER_CHAT,
    DASHBOARD,
    DESK,
    OFF,
    ON,
    TELEGRAM,
    FakeController,
    beat,
    command,
    on,
)

import home_keeper.api as api_module
from home_keeper.api import create_app
from home_keeper.models import CommandRefused
from home_keeper.store import Store

STALE_SECONDS = 15
LIGHT_OFF: Payload = {"state": "off"}
LIGHT_ON: Payload = {"state": "on", "brightness": 100}


@dataclass
class House:
    client: TestClient
    clock: list[datetime]
    controller: FakeController
    revision: int
    db_path: Path

    def report(
        self,
        identity: str,
        at: datetime,
        new: Payload,
        *,
        old: Payload = LIGHT_OFF,
        entity: str = "bulb-a",
        source: str = DESK,
    ) -> None:
        """Post a change event as the collector would, received at `at`."""
        payload = event(
            self.revision,
            identity,
            entity_id=entity,
            source_entity_id=source,
            event_time=at.isoformat(),
            received_at=at.isoformat(),
            old=old,
            new=new,
        )
        response = self.client.post("/internal/events", json=payload, headers=AUTH)
        assert response.json()["recorded"], response.text

    def snapshot(self, at: datetime, state: Payload, entity: str = "bulb-a") -> None:
        """Post a state snapshot as the collector would, received at `at`."""
        payload = observation(
            self.revision, at.isoformat(), entity_id=entity, source_entity_id=DESK, **state
        )
        self.client.post("/internal/baselines", json=payload, headers=AUTH).raise_for_status()

    def commands(self, **params: str | int) -> list[dict[str, Any]]:
        response = self.client.get("/api/commands", params=params)
        assert response.status_code == 200, response.text
        records: list[dict[str, Any]] = response.json()["commands"]
        return records


@contextmanager
def home(
    tmp_path: Path,
    *,
    reading: Payload | None = LIGHT_OFF,
    heartbeat: bool = True,
    wait: float = 0.3,
    retention_days: int = 30,
    read_at: datetime = NOW,
) -> Generator[House]:
    """bulb-a monitored with `reading` as its snapshot received at `read_at`; bulb-b discovered
    but not monitored."""
    clock = [NOW]
    controller = FakeController(clock)
    path = tmp_path / "commands.sqlite"
    app = create_app(
        path,
        TOKEN,
        now=lambda: clock[0],
        retention_days=retention_days,
        controller=controller,
        command_wait_seconds=wait,
        control_stale_seconds=STALE_SECONDS,
    )
    with TestClient(app) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH).raise_for_status()
        house = House(client, clock, controller, select(client), path)
        if reading is not None:
            house.snapshot(read_at, reading)
        if heartbeat:
            beat(client, NOW)
        yield house
        controller.join()


def refusal(response: httpx.Response) -> object:
    """The refusal a 4xx response carries: its fixed detail or its conflict code."""
    detail = response.json()["detail"]
    if isinstance(detail, str):
        return detail
    # A 409 must match the body the route declares in OpenAPI.
    assert response.status_code == 409, response.text
    return CommandRefused.model_validate_json(response.content).detail.code


def test_control_is_on_only_while_the_controller_heartbeat_is_fresh(tmp_path: Path) -> None:
    with home(tmp_path, heartbeat=False) as house:
        client = house.client
        assert client.get("/api/control").json() == {"state": "off", "heartbeat_at": None}
        refused = command(client, ON)
        assert (refused.status_code, refusal(refused)) == (409, "control_off")

        beat(client, NOW)
        control = client.get("/api/control").json()
        assert control["state"] == "on"
        assert datetime.fromisoformat(control["heartbeat_at"]) == NOW

        house.clock[0] = NOW + timedelta(seconds=STALE_SECONDS - 1)
        assert client.get("/api/control").json()["state"] == "on"

        house.clock[0] = NOW + timedelta(seconds=STALE_SECONDS + 1)
        stale = client.get("/api/control").json()
        assert stale["state"] == "off"
        assert datetime.fromisoformat(stale["heartbeat_at"]) == NOW
        refused = command(client, ON)
        assert (refused.status_code, refusal(refused)) == (409, "control_off")
        assert house.controller.sent == []

        beat(client, house.clock[0])
        assert client.get("/api/control").json()["state"] == "on"
        assert command(client, ON).status_code == 200
        assert len(house.controller.sent) == 1


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


def test_without_a_configured_controller_commands_are_refused_as_control_off(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(tmp_path / "none.sqlite", TOKEN, now=lambda: NOW)) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH).raise_for_status()
        revision = select(client)
        client.post(
            "/internal/baselines", json=observation(revision, state="off"), headers=AUTH
        ).raise_for_status()
        # Whether or not the heartbeat route accepts it, no controller means no control.
        client.post("/internal/control", json={"received_at": NOW.isoformat()}, headers=AUTH)
        refused = command(client, ON)
        assert (refused.status_code, refusal(refused)) == (409, "control_off")
        assert client.get("/api/commands").json()["commands"] == []


@pytest.mark.parametrize(
    ("entity", "action", "reading", "heartbeat", "status", "expected"),
    [
        # Catalog and support checks come before the control check.
        ("missing", ON, LIGHT_OFF, False, 404, "Unknown entity"),
        ("room", ON, LIGHT_OFF, False, 422, "Entity is unsupported"),
        ("plug", OFF, LIGHT_OFF, True, 422, "Entity is unsupported"),
        # The source entity ID is not a catalog ID.
        (DESK, ON, LIGHT_OFF, True, 404, "Unknown entity"),
        # Injected characters are only an unknown catalog ID.
        ("bulb-a;DROP TABLE events", ON, LIGHT_OFF, True, 404, "Unknown entity"),
        ("bulb-a' OR '1'='1", ON, LIGHT_OFF, True, 404, "Unknown entity"),
        ("<img src=x onerror=alert(1)>", ON, LIGHT_OFF, True, 404, "Unknown entity"),
        ("bulb-a%00", ON, LIGHT_OFF, True, 404, "Unknown entity"),
        ("bulb-a%0d%0aX-Home-Keeper: 1", ON, LIGHT_OFF, True, 404, "Unknown entity"),
        # Control is checked before the light's own state.
        ("bulb-b", ON, LIGHT_OFF, False, 409, "control_off"),
        ("bulb-a", ON, LIGHT_OFF, False, 409, "control_off"),
        ("bulb-b", ON, LIGHT_OFF, True, 409, "not_monitored"),
        ("bulb-a", ON, None, True, 409, "state_unknown"),
        ("bulb-a", ON, {"state": "unknown", "availability": "unknown"}, True, 409, "state_unknown"),
        (
            "bulb-a",
            ON,
            {"state": "unavailable", "availability": "unavailable"},
            True,
            409,
            "unavailable",
        ),
        (
            "bulb-a",
            OFF,
            {"state": "unavailable", "availability": "unavailable"},
            True,
            409,
            "unavailable",
        ),
        ("bulb-a", OFF, LIGHT_OFF, True, 409, "already_in_state"),
        ("bulb-a", ON, LIGHT_ON, True, 409, "already_in_state"),
        # 30 % is brightness round(76.5) = 77; 75 to 79 already match it.
        ("bulb-a", on(30), {"state": "on", "brightness": 76}, True, 409, "already_in_state"),
        ("bulb-a", on(30), {"state": "on", "brightness": 78}, True, 409, "already_in_state"),
    ],
)
def test_refusals_return_their_fixed_detail_in_order_and_nothing_is_sent_or_stored(
    tmp_path: Path,
    *,
    entity: str,
    action: Payload,
    reading: Payload | None,
    heartbeat: bool,
    status: int,
    expected: str,
) -> None:
    with home(tmp_path, reading=reading, heartbeat=heartbeat) as house:
        response = command(house.client, action, entity=entity)
        assert response.status_code == status, response.text
        assert refusal(response) == expected
        assert house.controller.sent == []
        assert house.commands() == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/lights/..%2Fselection%2Fbulb-a/command",
        "/api/lights/bulb-a%2F..%2Fbulb-b/command",
        "/api/lights//command",
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
            "/api/lights/bulb-a/command", json=body, headers={"X-Home-Keeper": "1"}
        )
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": "Invalid request"}
        assert house.controller.sent == []
        assert house.commands() == []


@pytest.mark.parametrize(
    ("reading", "action"),
    [
        (LIGHT_OFF, ON),
        (LIGHT_OFF, on(30)),
        (LIGHT_ON, OFF),
        # A different brightness is a change: 74 is three steps from 77.
        ({"state": "on", "brightness": 74}, on(30)),
        ({"state": "on", "brightness": 100}, on(30)),
        # A light with no recorded brightness cannot already be at 30 %.
        ({"state": "on", "color_mode": "onoff"}, on(30)),
    ],
)
def test_change_is_forwarded_with_the_source_entity_id_of_the_current_reading(
    tmp_path: Path, reading: Payload, action: Payload
) -> None:
    with home(tmp_path, reading=reading) as house:
        response = command(house.client, action)
        assert response.status_code == 200, response.text
        assert house.controller.sent == [(DESK, action)]


def test_forwarding_follows_the_source_entity_id_of_the_newest_reading(tmp_path: Path) -> None:
    with home(tmp_path) as house:
        # The latest event attributes bulb-a to a renamed Home Assistant entity.
        house.report("renamed", NOW, {"state": "off"}, old=LIGHT_ON, source="light.desk_2")
        assert command(house.client, on(55)).status_code == 200
        assert house.controller.sent == [("light.desk_2", on(55))]


def test_command_is_observed_when_a_matching_event_arrives_after_the_send(
    tmp_path: Path,
) -> None:
    with home(tmp_path, wait=5) as house:
        house.controller.deliver = lambda at: house.report(
            "after-send", at, {"state": "on", "brightness": 77}
        )
        response = command(house.client, on(30), TELEGRAM)
        assert response.status_code == 200, response.text
        record = response.json()
        assert set(record) == {
            "id",
            "entity_id",
            "command",
            "sender",
            "sent_at",
            "outcome",
            "event_id",
        }
        assert record["outcome"] == "observed"
        assert record["event_id"] == "after-send"
        assert record["entity_id"] == "bulb-a"
        assert record["command"] == on(30)
        assert record["sender"] == TELEGRAM
        # The send time is taken before the controller is called.
        assert datetime.fromisoformat(record["sent_at"]) == house.controller.sent_at[0] == NOW
        assert house.controller.sent == [(DESK, on(30))]
        house.controller.join()
        assert house.commands() == [record]
        events = house.client.get("/api/events", params={"entity_id": "bulb-a"}).json()
        assert "after-send" in {item["id"] for item in events["events"]}


def test_off_command_is_observed_from_an_off_event(tmp_path: Path) -> None:
    with home(tmp_path, reading=LIGHT_ON, wait=5) as house:
        house.controller.deliver = lambda at: house.report(
            "went-off", at, {"state": "off"}, old=LIGHT_ON
        )
        record = command(house.client, OFF, BROWSER_CHAT).json()
        assert (record["outcome"], record["event_id"]) == ("observed", "went-off")
        assert record["command"] == OFF
        assert record["sender"] == BROWSER_CHAT


def test_command_is_observed_from_a_snapshot_received_after_the_send(tmp_path: Path) -> None:
    with home(tmp_path, wait=5) as house:
        house.controller.deliver = lambda at: house.snapshot(at, {"state": "on"})
        record = command(house.client, ON).json()
        assert record["outcome"] == "observed"
        # A snapshot is evidence but not an event.
        assert record["event_id"] is None


@pytest.mark.parametrize(
    ("brightness", "outcome"),
    [(76, "observed"), (78, "observed"), (79, "observed"), (74, "not_observed")],
)
def test_brightness_within_two_steps_of_the_requested_percent_counts_as_observed(
    tmp_path: Path, brightness: int, outcome: str
) -> None:
    with home(tmp_path, wait=1.5) as house:
        house.controller.deliver = lambda at: house.report(
            "dimmed", at, {"state": "on", "brightness": brightness}
        )
        record = command(house.client, on(30)).json()
        assert record["outcome"] == outcome
        assert record["event_id"] == ("dimmed" if outcome == "observed" else None)
        assert house.controller.sent == [(DESK, on(30))]


def test_waiting_continues_past_a_non_matching_reading(tmp_path: Path) -> None:
    with home(tmp_path, wait=5) as house:

        def settle(at: datetime) -> None:
            house.report("ramping", at, {"state": "on", "brightness": 20})
            house.report(
                "settled",
                at,
                {"state": "on", "brightness": 77},
                old={"state": "on", "brightness": 20},
            )

        house.controller.deliver = settle
        record = command(house.client, on(30)).json()
        assert (record["outcome"], record["event_id"]) == ("observed", "settled")


def test_reading_received_before_the_send_never_counts(tmp_path: Path) -> None:
    with home(tmp_path, wait=0.5) as house:
        # The collector received this change before the send but delivers it late.
        house.controller.deliver = lambda _at: house.report(
            "late-delivery", NOW - timedelta(seconds=1), {"state": "on"}
        )
        record = command(house.client, ON).json()
        house.controller.join()
        assert record["outcome"] == "not_observed"
        assert record["event_id"] is None
        # The matching event was recorded after the send, but received before it.
        events = house.client.get("/api/events", params={"entity_id": "bulb-a"}).json()["events"]
        assert [(item["id"], item["new"]["state"]) for item in events] == [("late-delivery", "on")]


def test_snapshot_received_before_the_send_never_counts(tmp_path: Path) -> None:
    with home(tmp_path, wait=0.5, read_at=NOW - timedelta(seconds=10)) as house:
        # The collector read this state before the send but delivers it late.
        house.controller.deliver = lambda _at: house.snapshot(
            NOW - timedelta(seconds=1), {"state": "on"}
        )
        record = command(house.client, ON).json()
        house.controller.join()
        assert record["outcome"] == "not_observed"
        assert record["event_id"] is None
        baselines = house.client.get("/api/baselines", params={"entity_id": "bulb-a"}).json()
        assert baselines["baselines"][0]["state"] == "on"


class ShiftedTime:
    """The API module's `time`, with `monotonic` moved forward to reach its next cleanup."""

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


@pytest.mark.parametrize("reading", ["event", "snapshot"])
@pytest.mark.parametrize("cleanup", ["none", "store", "ingestion"])
def test_matching_reading_recorded_after_retention_cleanup_is_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reading: str, cleanup: str
) -> None:
    shifted = ShiftedTime()
    monkeypatch.setattr(api_module, "time", shifted)
    with home(tmp_path, retention_days=1, wait=1) as house:
        # The light's only stored history sits exactly at the retention boundary at the send.
        if reading == "event":
            house.report("old-off", NOW, {"state": "off"}, old=LIGHT_ON)
        house.clock[0] = NOW + timedelta(days=1)
        beat(house.client, house.clock[0])

        def expire() -> None:
            # Runs during the send, after the clock moved on: the old history expires.
            if cleanup == "store":
                with Store(house.db_path).transaction() as db:
                    Store.cleanup(db, (house.clock[0] - timedelta(days=1)).isoformat())
            elif cleanup == "ingestion":
                # The collector's next post runs the API's own periodic cleanup.
                shifted.offset = 61

        house.controller.on_send = expire

        def deliver(at: datetime) -> None:
            if reading == "event":
                house.report("new-on", at, {"state": "on"})
            else:
                house.snapshot(at, {"state": "on"})

        house.controller.deliver = deliver
        response = command(house.client, ON)
        house.controller.join()
        assert response.status_code == 200, response.text
        record = response.json()
        # The matching reading is stored; the history before it is gone.
        if reading == "event":
            events = house.client.get("/api/events").json()["events"]
            assert [item["id"] for item in events] == ["new-on"]
        else:
            [stored] = house.client.get("/api/baselines", params={"entity_id": "bulb-a"}).json()[
                "baselines"
            ]
            assert stored["state"] == "on"
            assert stored["received_at"] > record["sent_at"]
        assert (record["outcome"], record["event_id"]) == (
            "observed",
            "new-on" if reading == "event" else None,
        )
        assert house.commands() == [record]


def test_matching_reading_of_another_light_does_not_count(tmp_path: Path) -> None:
    with home(tmp_path, wait=0.5) as house:
        house.revision = select(house.client, "bulb-b")
        house.snapshot(NOW, LIGHT_OFF, entity="bulb-b")
        house.controller.deliver = lambda at: house.report(
            "other-light", at, {"state": "on"}, entity="bulb-b", source="light.other"
        )
        record = command(house.client, ON).json()
        assert record["outcome"] == "not_observed"


def test_no_matching_reading_within_the_wait_is_not_observed(tmp_path: Path) -> None:
    with home(tmp_path, wait=0.4) as house:
        started = time.monotonic()
        record = command(house.client, ON).json()
        elapsed = time.monotonic() - started
        assert record["outcome"] == "not_observed"
        assert record["event_id"] is None
        assert elapsed >= 0.4
        assert house.commands() == [record]


def test_home_assistant_rejecting_the_call_is_refused_without_waiting(tmp_path: Path) -> None:
    with home(tmp_path, wait=5) as house:
        house.controller.reply = "refused"
        started = time.monotonic()
        record = command(house.client, ON).json()
        assert time.monotonic() - started < 4
        assert (record["outcome"], record["event_id"]) == ("refused", None)
        assert house.controller.sent == [(DESK, ON)]
        assert house.commands() == [record]


def test_failed_controller_call_is_unreachable_and_still_audited(tmp_path: Path) -> None:
    with home(tmp_path, wait=5) as house:
        house.controller.unreachable = True
        started = time.monotonic()
        response = command(house.client, ON)
        assert time.monotonic() - started < 4
        assert response.status_code == 200, response.text
        record = response.json()
        assert (record["outcome"], record["event_id"]) == ("unreachable", None)
        assert house.commands() == [record]


def test_command_is_recorded_as_pending_while_the_controller_works(tmp_path: Path) -> None:
    with home(tmp_path) as house:
        seen: list[list[dict[str, Any]]] = []

        def look() -> None:
            # Another reader, on its own thread, while the send is in progress.
            reader = Thread(target=lambda: seen.append(house.commands()))
            reader.start()
            reader.join(timeout=5)

        house.controller.on_send = look
        record = command(house.client, on(30), TELEGRAM).json()
        assert len(seen) == 1, "reading the commands during the send failed"
        assert seen[0] == [{**record, "outcome": "pending", "event_id": None}]
        assert record["outcome"] == "not_observed"
        assert house.commands() == [record]


class SimulatedCrashError(Exception):
    """Stands in for the API process dying while the controller works."""


def test_command_left_pending_by_a_crash_is_interrupted_after_restart(tmp_path: Path) -> None:
    with home(tmp_path) as house:

        def crash() -> None:
            raise SimulatedCrashError

        house.controller.on_send = crash
        with pytest.raises(SimulatedCrashError):
            command(house.client, on(30), TELEGRAM)

        restarted = create_app(
            house.db_path,
            TOKEN,
            now=lambda: house.clock[0],
            controller=FakeController(house.clock),
            control_stale_seconds=STALE_SECONDS,
        )
        with TestClient(restarted) as client:
            rows = client.get("/api/commands").json()["commands"]
        assert [(row["outcome"], row["event_id"]) for row in rows] == [("interrupted", None)]
        assert (rows[0]["entity_id"], rows[0]["command"], rows[0]["sender"]) == (
            "bulb-a",
            on(30),
            TELEGRAM,
        )
        assert datetime.fromisoformat(rows[0]["sent_at"]) == house.controller.sent_at[0]


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
                        "/api/lights/bulb-a/command",
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
                        "/api/lights/bulb-a/command",
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


@pytest.mark.parametrize("sender", [DASHBOARD, BROWSER_CHAT, TELEGRAM])
def test_sender_round_trips_through_the_audit(tmp_path: Path, sender: Payload) -> None:
    with home(tmp_path) as house:
        house.controller.reply = "refused"
        record = command(house.client, ON, sender).json()
        assert record["sender"] == sender
        assert house.commands()[0]["sender"] == sender


def test_commands_page_newest_first_and_filter_by_send_time(tmp_path: Path) -> None:
    with home(tmp_path) as house:
        # Refused commands leave the light off, so the same command stays valid.
        house.controller.reply = "refused"
        sent: list[dict[str, Any]] = []
        for _ in range(3):
            # The commands span longer than the stale limit, so keep the heartbeat fresh.
            beat(house.client, house.clock[0])
            response = command(house.client, ON)
            assert response.status_code == 200, response.text
            sent.append(response.json())
            house.clock[0] += timedelta(seconds=9)
        times = [datetime.fromisoformat(record["sent_at"]) for record in sent]
        assert times == [NOW, NOW + timedelta(seconds=10), NOW + timedelta(seconds=20)]

        page = house.client.get("/api/commands", params={"limit": 2}).json()
        assert page["commands"] == [sent[2], sent[1]]
        assert page["next_cursor"]
        rest = house.client.get(
            "/api/commands", params={"limit": 2, "cursor": page["next_cursor"]}
        ).json()
        assert rest["commands"] == [sent[0]]
        assert rest["next_cursor"] is None

        assert house.commands(start=times[1].isoformat()) == [sent[2], sent[1]]
        assert house.commands(end=times[1].isoformat()) == [sent[1], sent[0]]
        assert house.commands(start=times[1].isoformat(), end=times[1].isoformat()) == [sent[1]]
        assert house.commands(limit=500) == [sent[2], sent[1], sent[0]]


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


def test_commands_older_than_retention_are_hidden_then_cleaned_up(tmp_path: Path) -> None:
    with home(tmp_path, retention_days=1) as house:
        house.controller.reply = "refused"
        old = command(house.client, ON).json()
        house.clock[0] = NOW + timedelta(days=1, seconds=30)
        beat(house.client, house.clock[0])
        recent = command(house.client, ON).json()

        page = house.client.get("/api/commands").json()
        assert page["commands"] == [recent]
        assert page["retained_since"] == (house.clock[0] - timedelta(days=1)).isoformat()

        store = Store(house.db_path)
        with store.transaction() as db:
            Store.cleanup(db, (NOW + timedelta(seconds=1)).isoformat())
        # Back inside the retention window, only a deleted row stays missing.
        house.clock[0] = NOW
        assert old not in house.commands()
        assert house.commands() == [recent]
