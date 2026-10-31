"""Home, health and commands read what Home Assistant reports now, through the reader.

Nothing runs between requests: each response comes from the reader at request time.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from support.api import BROWSER, NOW, TOKEN
from support.control import ON, FakeController, beat, command
from support.reader import FakeReader, live

from home_keeper.api import create_app
from home_keeper.models import DeviceKind, Entity
from home_keeper.reader import ReaderError

DESK = Entity(
    id="ha:desk",
    device_id="ha:desk-device",
    device_name="Desk lamp",
    name="Desk",
    device_kind="light",
    supported=True,
)
KETTLE = Entity(
    id="ha:kettle",
    device_id="ha:kettle-device",
    device_name="Kettle plug",
    name="Kettle",
    device_kind="switch",
    supported=True,
)
ROOM = Entity(
    id="ha:room",
    name="Living room",
    device_kind="light",
    supported=False,
    reason="Groups and rooms are not recorded",
)
DESK_ON = live("light.desk", {"state": "on", "brightness": 128})
KETTLE_OFF = live("switch.kettle", {"device_kind": "switch", "state": "off"})
# What /api/monitored adds to each catalog entry, and nothing else.
MONITORED_KEYS = {*Entity.model_fields, "source_entity_id", "state", "missing"}
# Safe codes for "cannot reach Home Assistant": the collector's codes plus the reader's own.
UNREACHABLE: list[ReaderError] = ["reader_unreachable", "connection", "authentication", "timeout"]


@dataclass
class House:
    client: TestClient
    reader: FakeReader
    controller: FakeController

    def monitored(self) -> dict[str, dict[str, Any]]:
        response = self.client.get("/api/monitored")
        assert response.status_code == 200, response.text
        return {entity["id"]: entity for entity in response.json()["entities"]}

    def select(self, entity_id: str, *, selected: bool = True) -> Response:
        return self.client.put(
            f"/api/selection/{entity_id}", json={"selected": selected}, headers=BROWSER
        )


@contextmanager
def house(database: str) -> Generator[House]:
    """Home Assistant reports the desk lamp on and the kettle off; nothing is monitored."""
    clock = [NOW]
    reader = FakeReader([DESK, KETTLE, ROOM], {DESK.id: DESK_ON, KETTLE.id: KETTLE_OFF})
    controller = FakeController(clock)
    app = create_app(
        database,
        TOKEN,
        now=lambda: clock[0],
        reader=reader,
        controller=controller,
        source_id="home_assistant",
        home_assistant_url="http://127.0.0.1:8123",
    )
    with TestClient(app) as client:
        yield House(client, reader, controller)


def unreachable_body(response: Response, error: ReaderError) -> None:
    """A 503 with the safe code and a message that names no address or credential."""
    assert response.status_code == 503, response.text
    body = response.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == error
    assert body["message"]
    assert "http" not in body["message"].lower()
    assert TOKEN not in body["message"]


def test_monitored_shows_what_home_assistant_reports_now_and_follows_each_change(
    database: str,
) -> None:
    with house(database) as home:
        assert home.select(DESK.id).is_success
        assert home.select(KETTLE.id).is_success

        monitored = home.monitored()
        assert list(monitored) == [DESK.id, KETTLE.id]
        desk = monitored[DESK.id]
        assert set(desk) == MONITORED_KEYS
        assert {key: desk[key] for key in Entity.model_fields} == DESK.model_dump()
        assert desk["source_entity_id"] == "light.desk"
        assert desk["missing"] is False
        assert {key: desk["state"][key] for key in ("device_kind", "state", "availability")} == {
            "device_kind": "light",
            "state": "on",
            "availability": "available",
        }
        assert desk["state"]["brightness"] == 128
        kettle = monitored[KETTLE.id]
        assert kettle["state"]["device_kind"] == "switch"
        assert kettle["state"]["state"] == "off"
        assert "brightness" not in kettle["state"]

        # Home Assistant changes the light; the very next read shows it.
        home.reader.set(DESK.id, "light.desk", {"state": "off"})
        assert home.monitored()[DESK.id]["state"]["state"] == "off"
        home.reader.set(DESK.id, "light.desk", {"state": "on", "brightness": 40})
        assert home.monitored()[DESK.id]["state"]["brightness"] == 40


def test_health_is_healthy_while_the_reader_and_home_assistant_answer(database: str) -> None:
    with house(database) as home:
        response = home.client.get("/api/health")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"source_id", "status", "error", "checked_at"}
        assert (body["source_id"], body["status"], body["error"]) == (
            "home_assistant",
            "healthy",
            None,
        )
        assert body["checked_at"]


@pytest.mark.parametrize("error", UNREACHABLE)
def test_unreachable_home_assistant_unknowns_every_state_but_still_lets_you_stop_monitoring(
    database: str, error: ReaderError
) -> None:
    with house(database) as home:
        assert home.select(DESK.id).is_success
        home.reader.error = error

        health = home.client.get("/api/health")
        assert health.status_code == 200, health.text
        assert (health.json()["status"], health.json()["error"]) == ("unreachable", error)

        # Every selected device stays listed, with no state: the old reading is not shown.
        [desk] = home.monitored().values()
        assert set(desk) == MONITORED_KEYS
        assert {key: desk[key] for key in ("id", "name", "device_kind", "state", "missing")} == {
            "id": DESK.id,
            "name": DESK.id,
            "device_kind": "light",
            "state": None,
            "missing": False,
        }

        unreachable_body(home.client.get("/api/catalog"), error)
        unreachable_body(home.select(KETTLE.id), error)
        assert home.client.get("/api/selection").json() == {"entity_ids": [DESK.id]}

        stopped = home.select(DESK.id, selected=False)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json() == {"entity_ids": []}
        assert home.monitored() == {}


def test_unreachable_home_assistant_refuses_a_command_as_state_unknown(database: str) -> None:
    with house(database) as home:
        assert home.select(DESK.id).is_success
        beat(home.client, NOW)
        home.reader.error = "connection"

        response = command(home.client, ON, entity=DESK.id)

        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "state_unknown"
        assert home.controller.sent == []
        assert home.client.get("/api/commands").json()["commands"] == []


def test_health_recovers_with_the_reader(database: str) -> None:
    with house(database) as home:
        assert home.select(DESK.id).is_success
        home.reader.error = "timeout"
        assert home.client.get("/api/health").json()["status"] == "unreachable"
        assert home.monitored()[DESK.id]["state"] is None

        home.reader.error = None
        assert home.client.get("/api/health").json()["error"] is None
        assert home.client.get("/api/health").json()["status"] == "healthy"
        assert home.monitored()[DESK.id]["state"]["state"] == "on"
        assert home.client.get("/api/catalog").status_code == 200


def test_a_device_home_assistant_no_longer_has_is_missing_and_can_be_stopped(
    database: str,
) -> None:
    with house(database) as home:
        assert home.select(DESK.id).is_success
        assert home.select(KETTLE.id).is_success
        home.reader.drop(DESK.id)

        monitored = home.monitored()
        assert (monitored[DESK.id]["missing"], monitored[DESK.id]["state"]) == (True, None)
        assert monitored[DESK.id]["device_kind"] == "light"
        assert monitored[KETTLE.id]["missing"] is False
        assert monitored[KETTLE.id]["state"]["state"] == "off"

        stopped = home.select(DESK.id, selected=False)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json() == {"entity_ids": [KETTLE.id]}


def reclassified(entity: Entity, kind: DeviceKind) -> Entity:
    """The device as the catalog lists it once Home Assistant sorts it as another kind."""
    return entity.model_copy(update={"device_kind": kind})


def test_a_monitored_device_follows_the_kind_the_catalog_gives_it_even_while_the_reader_is_down(
    database: str,
) -> None:
    with house(database) as home:
        # Monitored as a light.
        assert home.select(DESK.id).is_success
        assert home.monitored()[DESK.id]["device_kind"] == "light"

        # Home Assistant now sorts it as another kind; nobody selects it again.
        home.reader.entities = [reclassified(DESK, "switch"), KETTLE, ROOM]
        home.reader.set(DESK.id, "switch.desk", {"device_kind": "switch", "state": "on"})
        assert home.monitored()[DESK.id]["device_kind"] == "switch"

        # The kind kept for when Home Assistant cannot be read is the new one, not the old.
        home.reader.error = "connection"
        assert home.monitored()[DESK.id]["device_kind"] == "switch"


def test_a_device_the_catalog_reclassifies_as_a_state_device_leaves_usage_and_commands(
    database: str,
) -> None:
    with house(database) as home:
        assert home.select(DESK.id).is_success
        assert home.select(KETTLE.id).is_success
        beat(home.client, NOW)
        window = {"start": (NOW - timedelta(hours=1)).isoformat(), "end": NOW.isoformat()}
        used = home.client.get("/api/usage", params=window).json()["devices"]
        assert {device["entity_id"] for device in used} == {DESK.id, KETTLE.id}

        home.reader.entities = [reclassified(DESK, "state"), KETTLE, ROOM]
        home.reader.set(DESK.id, "climate.desk", {"device_kind": "state", "state": "heat"})

        assert home.monitored()[DESK.id]["device_kind"] == "state"
        used = home.client.get("/api/usage", params=window).json()["devices"]
        assert {device["entity_id"] for device in used} == {KETTLE.id}
        refused = command(home.client, ON, entity=DESK.id)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "not_supported"
        assert home.controller.sent == []
