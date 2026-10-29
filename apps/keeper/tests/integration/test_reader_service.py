"""The reader service with its live backend, over the fake Home Assistant websocket and REST.

The reader holds the Home Assistant token. These tests pin what it answers on its internal
routes and that it sends Home Assistant nothing beyond its fixed reads.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from support.home_assistant import (
    FakeSocket,
    Frame,
    StateServer,
    hue_room,
    light_state,
    registry_entry,
    use_transport,
)

from home_keeper.home_assistant import HomeAssistantConnector
from home_keeper.reader_service import HomeAssistantBackend, create_reader_app

SERVICE_TOKEN = "reader-service-credential-sentinel"
HA_TOKEN = "ha-owner-token-sentinel"
AUTH = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
# Every websocket frame type the reader may send: authentication and the two registry reads.
ALLOWED_FRAMES = {"auth", "config/entity_registry/list", "config/device_registry/list"}
NO_TRANSLATION = {"platform": "zha", "translation_key": None}
ENTITIES: list[Frame] = [
    registry_entry("hue", "light.bulb"),
    registry_entry("room", "light.living_room", translation_key="hue_grouped_light"),
    registry_entry("disabled", "light.disabled", disabled_by="user", **NO_TRANSLATION),
    registry_entry("plug", "switch.plug", **NO_TRANSLATION),
    registry_entry("loose", "switch.template", device_id=None, **NO_TRANSLATION),
    registry_entry("sensor", "sensor.power", **NO_TRANSLATION),
]
DEVICES: list[Frame] = [{"id": "device1", "name": "Desk lamp"}]
STATES: list[Frame] = [
    light_state("light.bulb", brightness=120, color_mode="brightness"),
    hue_room("light.living_room"),
    light_state("switch.plug") | {"state": "off", "attributes": {"friendly_name": "Plug"}},
    light_state("sensor.power"),
]


@dataclass
class Home:
    """A fake Home Assistant: each websocket connect gets a fresh scripted socket."""

    entities: list[Frame]
    devices: list[Frame]
    server: StateServer
    sockets: list[FakeSocket] = field(default_factory=list[FakeSocket])
    # When set, the websocket connect is refused, as when Home Assistant is down.
    down: bool = False

    def connect(self, *_args: object, **_kwargs: object) -> FakeSocket:
        if self.down:
            raise ConnectionRefusedError
        socket = FakeSocket(self.entities, self.devices)
        self.sockets.append(socket)
        return socket

    def frames(self) -> list[Frame]:
        return [frame for socket in self.sockets for frame in socket.sent]


def connector_for(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> HomeAssistantConnector:
    token = tmp_path / "ha-token"
    token.write_text(HA_TOKEN)
    monkeypatch.setattr("home_keeper.home_assistant.connect", home.connect)
    connector = HomeAssistantConnector("http://homeassistant:8123", token)
    use_transport(connector, home.server)
    return connector


@pytest.fixture
def home() -> Home:
    return Home(ENTITIES, DEVICES, StateServer(STATES))


@pytest.fixture
def reader(home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    connector = connector_for(home, tmp_path, monkeypatch)
    with TestClient(
        create_reader_app(HomeAssistantBackend(lambda: connector), SERVICE_TOKEN)
    ) as client:
        yield client
    connector.close()


def test_catalog_projects_the_registry_by_todays_support_rules(reader: TestClient) -> None:
    response = reader.get("/internal/catalog", headers=AUTH)
    assert response.status_code == 200, response.text
    entities = response.json()["entities"]

    assert {entity["id"]: (entity["supported"], entity["reason"]) for entity in entities} == {
        "ha:hue": (True, None),
        "ha:room": (False, "Groups and rooms are not recorded"),
        "ha:disabled": (False, "Disabled in Home Assistant"),
        "ha:plug": (True, None),
        "ha:loose": (False, "Switches without a device are not recorded"),
        "ha:sensor": (False, "Only lights, plugs and switches are supported"),
    }
    hue = next(entity for entity in entities if entity["id"] == "ha:hue")
    assert hue == {
        "id": "ha:hue",
        "device_id": "ha:device1",
        "device_name": "Desk lamp",
        # The registry entry has no name of its own.
        "name": "Unnamed entity",
        "device_kind": "light",
        "supported": True,
        "reason": None,
    }
    assert "PRIVATE" not in response.text


def test_catalog_matches_the_collectors_discovery_of_the_same_home(
    reader: TestClient, home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = reader.get("/internal/catalog", headers=AUTH).json()["entities"]
    (tmp_path / "direct").mkdir()
    direct = connector_for(home, tmp_path / "direct", monkeypatch)
    try:
        discovered = [entity.model_dump() for entity in direct.discover().entities]
    finally:
        direct.close()
    assert served == discovered


def test_states_reads_each_requested_device_now_and_marks_a_missing_one(
    reader: TestClient, home: Home
) -> None:
    response = reader.get(
        "/internal/states",
        params=[("entity_id", "ha:hue"), ("entity_id", "ha:plug"), ("entity_id", "ha:gone")],
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    states = response.json()["states"]
    assert set(states) == {"ha:hue", "ha:plug", "ha:gone"}
    assert states["ha:gone"] is None
    hue = states["ha:hue"]
    assert hue["source_entity_id"] == "light.bulb"
    assert {key: hue["reading"][key] for key in ("device_kind", "state", "availability")} == {
        "device_kind": "light",
        "state": "on",
        "availability": "available",
    }
    assert hue["reading"]["brightness"] == 120
    plug = states["ha:plug"]
    assert plug["source_entity_id"] == "switch.plug"
    assert (plug["reading"]["device_kind"], plug["reading"]["state"]) == ("switch", "off")
    assert "brightness" not in plug["reading"]

    # A state is never cached: Home Assistant's next answer is the next response.
    home.server.states["light.bulb"] = light_state("light.bulb") | {"state": "off"}
    again = reader.get("/internal/states", params={"entity_id": "ha:hue"}, headers=AUTH).json()
    assert again["states"]["ha:hue"]["reading"]["state"] == "off"


def test_health_follows_home_assistants_reachability(reader: TestClient, home: Home) -> None:
    assert reader.get("/internal/health", headers=AUTH).json() == {"status": "healthy"}
    assert ("GET", "/api/") in home.server.requests

    home.server.api_status = 401
    rejected = reader.get("/internal/health", headers=AUTH)
    assert (rejected.status_code, rejected.json()) == (503, {"error": "authentication"})


def test_unreachable_home_assistant_is_a_503_with_a_safe_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to http://homeassistant:8123")

    home = Home(ENTITIES, DEVICES, StateServer(STATES), down=True)
    connector = connector_for(home, tmp_path, monkeypatch)
    use_transport(connector, refuse)
    with TestClient(
        create_reader_app(HomeAssistantBackend(lambda: connector), SERVICE_TOKEN)
    ) as client:
        for path in ("/internal/health", "/internal/catalog", "/internal/states?entity_id=ha:hue"):
            response = client.get(path, headers=AUTH)
            assert response.status_code == 503, response.text
            assert response.json() == {"error": "connection"}
            assert "homeassistant" not in response.text
            assert HA_TOKEN not in response.text
    connector.close()


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-credential"}, {"Authorization": SERVICE_TOKEN}],
)
@pytest.mark.parametrize(
    "path", ["/internal/health", "/internal/catalog", "/internal/states?entity_id=ha:hue"]
)
def test_every_route_needs_the_service_credential(
    reader: TestClient, home: Home, headers: dict[str, str], path: str
) -> None:
    sent = (len(home.frames()), len(home.server.requests))
    response = reader.get(path, headers=headers)
    assert response.status_code == 401, response.text
    # Nothing reached Home Assistant for a request without the credential.
    assert (len(home.frames()), len(home.server.requests)) == sent


def test_the_reader_sends_home_assistant_only_its_fixed_reads(
    reader: TestClient, home: Home
) -> None:
    reader.get("/internal/health", headers=AUTH)
    reader.get("/internal/catalog", headers=AUTH)
    reader.get("/internal/states", params={"entity_id": "ha:hue"}, headers=AUTH)
    # A path-shaped id must not become another Home Assistant path.
    reader.get("/internal/states", params={"entity_id": "../services/light"}, headers=AUTH)

    assert {frame["type"] for frame in home.frames()} <= ALLOWED_FRAMES
    assert home.frames(), "the catalog was not read from the registries"
    for method, path in home.server.requests:
        assert method == "GET"
        assert path == "/api/" or path.startswith("/api/states/"), path
        assert "services" not in path
    # No other HTTP method or route exists on the reader for a caller to reach Home Assistant.
    for response in (
        reader.post("/internal/states", headers=AUTH),
        reader.put("/internal/states", headers=AUTH),
        reader.delete("/internal/states", headers=AUTH),
    ):
        assert response.status_code in (404, 405)
    assert reader.post("/internal/command", headers=AUTH, json={}).status_code in (404, 405)
