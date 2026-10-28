"""Plug and switch commands: the light rules, the switch body the controller gets, the audit."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from support.api import AUTH, CATALOG_ENTITIES, NOW, TOKEN, Payload, event, observation, select
from support.control import (
    DASHBOARD,
    OFF,
    ON,
    TELEGRAM,
    FakeController,
    beat,
    command,
    forwarded,
    on,
)

from home_keeper.api import create_app
from home_keeper.models import CommandRefused

KETTLE = "switch.kettle"
PLUG: Payload = {
    "id": "plug-a",
    "device_id": "device-plug-a",
    "device_name": "Kettle plug",
    "name": "Kettle",
    "device_kind": "switch",
    "supported": True,
    "reason": None,
}
SWITCH: Payload = {"device_kind": "switch"}
PLUG_OFF: Payload = {"state": "off"} | SWITCH
PLUG_ON: Payload = {"state": "on"} | SWITCH


@dataclass
class House:
    client: TestClient
    controller: FakeController
    revision: int

    def report(self, identity: str, at: datetime, old: Payload, new: Payload) -> None:
        """Post a change of the plug as the collector would, received at `at`."""
        payload = event(
            self.revision,
            identity,
            entity_id="plug-a",
            source_entity_id=KETTLE,
            event_time=at.isoformat(),
            received_at=at.isoformat(),
            old=old,
            new=new,
        )
        response = self.client.post("/internal/events", json=payload, headers=AUTH)
        assert response.json()["recorded"], response.text

    def snapshot(self, at: datetime, state: Payload) -> None:
        """Post a plug snapshot as the collector would, received at `at`."""
        payload = observation(
            self.revision,
            at.isoformat(),
            entity_id="plug-a",
            source_entity_id=KETTLE,
            **(SWITCH | state),
        )
        self.client.post("/internal/baselines", json=payload, headers=AUTH).raise_for_status()

    def commands(self) -> list[dict[str, Any]]:
        response = self.client.get("/api/commands")
        assert response.status_code == 200, response.text
        records: list[dict[str, Any]] = response.json()["commands"]
        return records


@contextmanager
def home(
    tmp_path: Path,
    *,
    reading: Payload | None = PLUG_OFF,
    monitored: bool = True,
    heartbeat: bool = True,
    wait: float = 0.3,
) -> Generator[House]:
    """The kettle plug `plug-a` (Home Assistant `switch.kettle`) monitored with `reading` as
    its snapshot, beside the test lights."""
    clock = [NOW]
    controller = FakeController(clock)
    app = create_app(
        tmp_path / "switch-commands.sqlite",
        TOKEN,
        now=lambda: clock[0],
        controller=controller,
        command_wait_seconds=wait,
        control_stale_seconds=15,
    )
    catalog = {"source_id": "demo", "entities": [*CATALOG_ENTITIES, PLUG]}
    with TestClient(app) as client:
        client.post("/internal/catalog", json=catalog, headers=AUTH).raise_for_status()
        revision = select(client, "bulb-a")
        if monitored:
            revision = select(client, "plug-a")
        house = House(client, controller, revision)
        if reading is not None:
            house.snapshot(NOW, reading)
        if heartbeat:
            beat(client, NOW)
        yield house
        controller.join()


def refusal(response: httpx.Response) -> object:
    assert response.status_code == 409, response.text
    return CommandRefused.model_validate_json(response.content).detail.code


@pytest.mark.parametrize(
    ("action", "reading", "monitored", "heartbeat", "expected"),
    [
        (ON, PLUG_OFF, True, False, "control_off"),
        # The recorder takes no reading of a plug that is not monitored.
        (ON, None, False, True, "not_monitored"),
        (ON, None, True, True, "state_unknown"),
        (ON, {"state": "unknown", "availability": "unknown"} | SWITCH, True, True, "state_unknown"),
        (
            ON,
            {"state": "unavailable", "availability": "unavailable"} | SWITCH,
            True,
            True,
            "unavailable",
        ),
        (OFF, PLUG_OFF, True, True, "already_in_state"),
        (ON, PLUG_ON, True, True, "already_in_state"),
        # A plug has no brightness, so no brightness command can be carried out on it.
        (on(30), PLUG_OFF, True, True, "not_supported"),
        (on(100), PLUG_OFF, True, True, "not_supported"),
    ],
)
def test_plug_refusals_follow_the_light_rules_and_nothing_is_sent_or_stored(
    tmp_path: Path,
    *,
    action: Payload,
    reading: Payload | None,
    monitored: bool,
    heartbeat: bool,
    expected: str,
) -> None:
    with home(tmp_path, reading=reading, monitored=monitored, heartbeat=heartbeat) as house:
        response = command(house.client, action, entity="plug-a")
        assert refusal(response) == expected
        assert house.controller.sent == []
        assert house.commands() == []


@pytest.mark.parametrize(("reading", "action"), [(PLUG_OFF, ON), (PLUG_ON, OFF)])
def test_plug_change_is_forwarded_as_a_switch_command_to_its_source_entity(
    tmp_path: Path, reading: Payload, action: Payload
) -> None:
    with home(tmp_path, reading=reading) as house:
        response = command(house.client, action, entity="plug-a")
        assert response.status_code == 200, response.text
        assert house.controller.sent == [
            {"device_kind": "switch", "source_entity_id": KETTLE, "command": action}
        ]
        assert response.json()["device_kind"] == "switch"


def test_plug_on_is_recorded_with_its_sender_and_observed_from_the_switch_event(
    tmp_path: Path,
) -> None:
    with home(tmp_path, wait=5) as house:
        house.controller.deliver = lambda at: house.report("kettle-on", at, PLUG_OFF, PLUG_ON)
        response = command(house.client, ON, TELEGRAM, entity="plug-a")
        house.controller.join()
        assert response.status_code == 200, response.text
        record = response.json()
        assert house.controller.sent == [forwarded(ON, KETTLE)]
        assert {key: record[key] for key in record if key not in ("id", "sent_at")} == {
            "entity_id": "plug-a",
            "device_kind": "switch",
            "command": ON,
            "sender": TELEGRAM,
            "outcome": "observed",
            "event_id": "kettle-on",
        }
        assert house.commands() == [record]


def test_plug_off_is_observed_from_a_switch_snapshot_after_the_send(tmp_path: Path) -> None:
    with home(tmp_path, reading=PLUG_ON, wait=5) as house:
        house.controller.deliver = lambda at: house.snapshot(at, PLUG_OFF)
        response = command(house.client, OFF, DASHBOARD, entity="plug-a")
        house.controller.join()
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "observed"
        assert response.json()["event_id"] is None


def test_plug_that_does_not_change_is_not_observed(tmp_path: Path) -> None:
    with home(tmp_path) as house:
        # The plug reports again, still off: Home Assistant took the call but nothing changed.
        house.controller.deliver = lambda at: house.snapshot(at, PLUG_OFF)
        response = command(house.client, ON, entity="plug-a")
        house.controller.join()
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "not_observed"
        assert [record["outcome"] for record in house.commands()] == ["not_observed"]
