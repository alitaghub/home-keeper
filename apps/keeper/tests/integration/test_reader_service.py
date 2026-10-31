"""The reader service with its live backend, over the fake Home Assistant websocket and REST.

The reader holds the Home Assistant token. These tests pin what it answers on its internal
routes and that it sends Home Assistant nothing beyond its fixed reads.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from support.home_assistant import (
    FakeSocket,
    Frame,
    StateServer,
    entity_state,
    history_row,
    hue_room,
    light_state,
    realistic_device,
    realistic_entity,
    registry_entry,
    statistics_row,
    use_transport,
)

from home_keeper import reader as reader_module
from home_keeper.home_assistant import HomeAssistantConnector
from home_keeper.reader import HttpReader
from home_keeper.reader_service import HomeAssistantBackend, create_reader_app

SERVICE_TOKEN = "reader-service-credential-sentinel"
HA_TOKEN = "ha-owner-token-sentinel"
AUTH = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
# Every websocket frame type the reader may send: authentication, the two registry reads, the
# language and translation reads, and the history read.
ALLOWED_FRAMES = {
    "auth",
    "config/entity_registry/list",
    "config/device_registry/list",
    "get_config",
    "frontend/get_translations",
    "history/history_during_period",
    "recorder/statistics_during_period",
}
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
START = datetime(2026, 10, 13, 9, tzinfo=UTC)
END = START + timedelta(hours=1)
WINDOW = {"start": START.isoformat(), "end": END.isoformat()}
TIMES = {"last_changed", "last_updated", "last_reported"}


def at(seconds: float) -> float:
    """A Unix time `seconds` after START, as Home Assistant's compressed rows carry it."""
    return START.timestamp() + seconds


# Home Assistant's recorder rows, as `history/history_during_period` returns them.
HISTORY: dict[str, list[Frame]] = {
    "light.bulb": [
        # The state at START: `lc` is left out because it equals `lu`.
        history_row(
            "on",
            at(0.25),
            brightness=120,
            color_mode="brightness",
            friendly_name="Desk",
            effect="colorloop",
            untrusted="PRIVATE_SENTINEL",
        ),
        # Only attributes changed, so `lu` moves and `lc` is sent.
        history_row(
            "on",
            at(0.25),
            at(60.5),
            brightness=200,
            color_mode="xy",
            xy_color=[0.4, 0.3],
            supported_color_modes=["xy"],
        ),
        # No attributes at all.
        {"s": "unavailable", "lu": at(120)},
    ],
    "switch.plug": [
        history_row("off", at(0), friendly_name="Plug", current_power_w=5.0),
        history_row("on", at(30), brightness=255),
    ],
}


@dataclass
class Home:
    """A fake Home Assistant: each websocket connect gets a fresh scripted socket."""

    entities: list[Frame]
    devices: list[Frame]
    server: StateServer
    history: dict[str, list[Frame]] = field(default_factory=dict[str, list[Frame]])
    translations: dict[str, str] = field(default_factory=dict[str, str])
    statistics: dict[str, list[Frame]] = field(default_factory=dict[str, list[Frame]])
    sockets: list[FakeSocket] = field(default_factory=list[FakeSocket])
    # When set, the websocket connect is refused, as when Home Assistant is down.
    down: bool = False

    def connect(self, *_args: object, **_kwargs: object) -> FakeSocket:
        if self.down:
            raise ConnectionRefusedError
        socket = FakeSocket(
            self.entities,
            self.devices,
            self.history,
            statistics=self.statistics,
            translations=self.translations,
        )
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
    return Home(ENTITIES, DEVICES, StateServer(STATES), HISTORY)


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
        # A sensor on a device is a generic state device.
        "ha:sensor": (True, None),
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


def test_a_5000_entity_catalog_passes_through_the_reader_service_and_http_reader(
    home: Home, reader: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    lights = [realistic_entity(number) for number in range(4000)]
    plugs = [
        realistic_entity(number, f"switch.plug_{number}", platform="zha", translation_key=None)
        for number in range(4000, 4500)
    ]
    sensors = [
        realistic_entity(number, f"sensor.power_{number}", platform="zha", translation_key=None)
        for number in range(4500, 5000)
    ]
    home.entities = [*lights, *plugs, *sensors]
    home.devices = [realistic_device(number) for number in range(5000)]

    served = reader.get("/internal/catalog", headers=AUTH)
    assert served.status_code == 200, served.text
    assert len(served.json()["entities"]) == 5000

    def forward(request: httpx.Request) -> httpx.Response:
        answer = reader.get(request.url.raw_path.decode(), headers=AUTH)
        return httpx.Response(answer.status_code, content=answer.content)

    transport = httpx.MockTransport(forward)
    monkeypatch.setattr(reader_module.httpx, "Client", partial(httpx.Client, transport=transport))
    entities = HttpReader("http://reader.internal", SERVICE_TOKEN).catalog()
    assert len(entities) == 5000
    assert sum(entity.supported for entity in entities) == 5000
    assert ("GET", "/api/states") in home.server.requests
    assert all(path in ("/api/", "/api/states") for _, path in home.server.requests)


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


def test_states_of_several_devices_take_one_home_assistant_read(
    reader: TestClient, home: Home
) -> None:
    # Discovery reads each candidate's state once to spot groups; that is not a state read.
    assert reader.get("/internal/catalog", headers=AUTH).status_code == 200
    home.server.requests.clear()

    response = reader.get(
        "/internal/states",
        params=[("entity_id", "ha:hue"), ("entity_id", "ha:plug"), ("entity_id", "ha:gone")],
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert home.server.requests == [("GET", "/api/states")]


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
        for path, params in (
            ("/internal/health", {}),
            ("/internal/catalog", {}),
            ("/internal/states", {"entity_id": "ha:hue"}),
            ("/internal/history", {"entity_id": "ha:hue", **WINDOW}),
            ("/internal/words", {"entity_id": "ha:hue"}),
            ("/internal/statistics", {"entity_id": "ha:hue", **WINDOW}),
        ):
            response = client.get(path, params=params, headers=AUTH)
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
    "path",
    [
        "/internal/health",
        "/internal/catalog",
        "/internal/states?entity_id=ha:hue",
        "/internal/words?entity_id=ha:hue",
        "/internal/statistics?entity_id=ha:hue&start=2026-10-13T09:00:00Z&end=2026-10-13T10:00:00Z",
        "/internal/history?entity_id=ha:hue&start=2026-10-13T09:00:00Z&end=2026-10-13T10:00:00Z",
    ],
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
    reader.get("/internal/history", params={"entity_id": "ha:hue", **WINDOW}, headers=AUTH)
    reader.get("/internal/statistics", params={"entity_id": "ha:sensor", **WINDOW}, headers=AUTH)
    # A path-shaped id must not become another Home Assistant path.
    reader.get("/internal/states", params={"entity_id": "../services/light"}, headers=AUTH)

    assert {frame["type"] for frame in home.frames()} <= ALLOWED_FRAMES
    assert home.frames(), "the catalog was not read from the registries"
    for method, path in home.server.requests:
        assert method == "GET"
        assert path in ("/api/", "/api/states"), path
        assert "services" not in path
    # No other HTTP method or route exists on the reader for a caller to reach Home Assistant.
    for response in (
        reader.post("/internal/states", headers=AUTH),
        reader.put("/internal/states", headers=AUTH),
        reader.delete("/internal/states", headers=AUTH),
    ):
        assert response.status_code in (404, 405)
    assert reader.post("/internal/command", headers=AUTH, json={}).status_code in (404, 405)


def history_frames(home: Home) -> list[Frame]:
    return [frame for frame in home.frames() if frame["type"] == "history/history_during_period"]


def without_times(reading: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in reading.items() if key not in TIMES and value is not None}


def time_of(reading: dict[str, Any], field: str) -> datetime:
    return datetime.fromisoformat(reading[field])


def test_history_asks_home_assistant_once_for_every_change_in_the_window(
    reader: TestClient, home: Home
) -> None:
    response = reader.get(
        "/internal/history",
        params=[("entity_id", "ha:hue"), ("entity_id", "ha:plug"), *WINDOW.items()],
        headers=AUTH,
    )
    assert response.status_code == 200, response.text

    [frame] = history_frames(home)
    assert isinstance(frame["id"], int)
    assert {key: value for key, value in frame.items() if key not in ("id", "entity_ids")} | {
        "start_time": datetime.fromisoformat(frame["start_time"]),
        "end_time": datetime.fromisoformat(frame["end_time"]),
    } == {
        "type": "history/history_during_period",
        "start_time": START,
        "end_time": END,
        # The start-time state lets the first change in the window have its old reading.
        "include_start_time_state": True,
        # Lights are not significant to Home Assistant: without this, brightness and color
        # changes are left out.
        "significant_changes_only": False,
        "minimal_response": False,
        "no_attributes": False,
    }
    assert sorted(frame["entity_ids"]) == ["light.bulb", "switch.plug"]


def test_history_parses_home_assistants_compressed_rows_into_readings(
    reader: TestClient,
) -> None:
    response = reader.get(
        "/internal/history",
        params=[("entity_id", "ha:hue"), ("entity_id", "ha:plug"), *WINDOW.items()],
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    history = response.json()["history"]

    assert set(history) == {"ha:hue", "ha:plug"}
    bulb = history["ha:hue"]
    assert [row["source_entity_id"] for row in bulb] == ["light.bulb"] * 3
    readings = [row["reading"] for row in bulb]
    # Only the approved light fields are kept; names, effects and anything else are dropped.
    assert [without_times(reading) for reading in readings] == [
        {
            "device_kind": "light",
            "state": "on",
            "label": "on",
            "availability": "available",
            "brightness": 120,
            "color_mode": "brightness",
        },
        {
            "device_kind": "light",
            "state": "on",
            "label": "on",
            "availability": "available",
            "brightness": 200,
            "color_mode": "xy",
            "xy_color": [0.4, 0.3],
        },
        {
            "device_kind": "light",
            "state": "unavailable",
            "label": "unavailable",
            "availability": "unavailable",
        },
    ]
    assert "PRIVATE" not in response.text
    expected_times = [(0.25, 0.25), (0.25, 60.5), (120, 120)]
    assert [
        (time_of(reading, "last_changed"), time_of(reading, "last_updated")) for reading in readings
    ] == [
        (datetime.fromtimestamp(at(changed), UTC), datetime.fromtimestamp(at(updated), UTC))
        for changed, updated in expected_times
    ]

    # A switch keeps its state and availability only, even when Home Assistant sends more.
    plug = [row["reading"] for row in history["ha:plug"]]
    assert [without_times(reading) for reading in plug] == [
        {"device_kind": "switch", "state": "off", "label": "off", "availability": "available"},
        {"device_kind": "switch", "state": "on", "label": "on", "availability": "available"},
    ]
    assert [time_of(reading, "last_updated") for reading in plug] == [
        datetime.fromtimestamp(at(0), UTC),
        datetime.fromtimestamp(at(30), UTC),
    ]


def test_history_of_an_entity_home_assistant_does_not_have_is_empty(
    reader: TestClient, home: Home
) -> None:
    # Home Assistant knows the bulb but has recorded nothing for it in the window.
    home.history = {}
    response = reader.get(
        "/internal/history",
        params=[("entity_id", "ha:hue"), ("entity_id", "ha:gone"), *WINDOW.items()],
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"history": {"ha:hue": [], "ha:gone": []}}
    for frame in history_frames(home):
        assert frame["entity_ids"] == ["light.bulb"]


def test_history_keeps_working_after_other_websocket_requests(
    reader: TestClient, home: Home
) -> None:
    assert reader.get("/internal/catalog", headers=AUTH).status_code == 200
    for _ in range(3):
        response = reader.get(
            "/internal/history", params={"entity_id": "ha:hue", **WINDOW}, headers=AUTH
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["history"]["ha:hue"]) == 3
    # Home Assistant refused no request for reusing an ID.
    assert len(history_frames(home)) == 3


CLIMATE = registry_entry("climate", "climate.hall", platform="demo", translation_key=None)
ATTIC = registry_entry("attic", "climate.attic", platform="demo", translation_key=None)
CLIMATE_TRANSLATIONS = {
    "component.climate.entity_component._.state.heat": "Heat",
    "component.climate.entity_component._.state.off": "Off",
}


@pytest.fixture
def state_home(home: Home) -> Home:
    """The home plus two thermostats, one with translated states."""
    home.entities = [*ENTITIES, CLIMATE, ATTIC]
    home.translations = CLIMATE_TRANSLATIONS
    home.server.states["climate.hall"] = entity_state(
        "climate.hall", "heat", temperature=21, secret="PRIVATE_SENTINEL"
    )
    home.server.states["climate.attic"] = entity_state("climate.attic", "frost_guard")
    return home


def test_catalog_lists_a_device_backed_climate_entity_as_a_supported_state_device(
    reader: TestClient, state_home: Home
) -> None:
    entities = reader.get("/internal/catalog", headers=AUTH).json()["entities"]

    climate = next(entity for entity in entities if entity["id"] == "ha:climate")
    assert (climate["supported"], climate["reason"], climate["device_kind"]) == (
        True,
        None,
        "state",
    )


def test_states_returns_a_state_reading_with_its_translated_label_and_no_attributes(
    reader: TestClient, state_home: Home
) -> None:
    response = reader.get(
        "/internal/states",
        params=[("entity_id", "ha:climate"), ("entity_id", "ha:attic"), ("entity_id", "ha:plug")],
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    states = response.json()["states"]

    climate = states["ha:climate"]
    assert climate["source_entity_id"] == "climate.hall"
    assert without_times(climate["reading"]) == {
        "device_kind": "state",
        "state": "heat",
        "availability": "available",
        "label": "Heat",
    }
    # Home Assistant has no translation for this state, so the label is the state itself.
    assert states["ha:attic"]["reading"]["label"] == "frost_guard"
    # Lights and switches carry a label too; this plug's `off` has no translation either.
    assert states["ha:plug"]["reading"]["label"] == "off"
    assert "PRIVATE" not in response.text
    assert "temperature" not in response.text


def test_history_of_a_state_device_carries_labels_and_is_read_without_attributes(
    reader: TestClient, state_home: Home
) -> None:
    state_home.history = {
        "climate.hall": [
            {"s": "heat", "a": {"temperature": 21}, "lu": at(0)},
            {"s": "off", "a": {"temperature": 18}, "lu": at(30)},
        ]
    }
    response = reader.get(
        "/internal/history", params={"entity_id": "ha:climate", **WINDOW}, headers=AUTH
    )
    assert response.status_code == 200, response.text

    readings = [row["reading"] for row in response.json()["history"]["ha:climate"]]
    assert [without_times(reading) for reading in readings] == [
        {"device_kind": "state", "state": "heat", "availability": "available", "label": "Heat"},
        {"device_kind": "state", "state": "off", "availability": "available", "label": "Off"},
    ]
    assert "temperature" not in response.text
    [frame] = history_frames(state_home)
    assert frame["entity_ids"] == ["climate.hall"]
    assert frame["no_attributes"] is True


DOOR_WORDS = {
    "component.binary_sensor.entity_component.door.state.on": "Open",
    "component.binary_sensor.entity_component.door.state.off": "Closed",
}


@pytest.fixture
def door_home(home: Home) -> Home:
    """The home plus a door sensor with translated states and a fan with none."""
    home.entities = [
        *ENTITIES,
        registry_entry("door", "binary_sensor.front", platform="demo", translation_key=None),
        registry_entry("fan", "fan.ceiling", platform="demo", translation_key=None),
    ]
    home.translations = DOOR_WORDS
    home.server.states["binary_sensor.front"] = entity_state(
        "binary_sensor.front", "off", device_class="door"
    )
    home.server.states["fan.ceiling"] = entity_state("fan.ceiling", "on")
    return home


def test_words_gives_a_door_its_translated_words_and_raw_words_where_there_are_none(
    reader: TestClient, door_home: Home
) -> None:
    # The catalog read teaches the reader each device's class and Home Assistant's words.
    assert reader.get("/internal/catalog", headers=AUTH).status_code == 200

    response = reader.get(
        "/internal/words",
        params=[
            ("entity_id", "ha:door"),
            ("entity_id", "ha:fan"),
            ("entity_id", "ha:gone"),
        ],
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "words": {
            "ha:door": {"on": "Open", "off": "Closed"},
            # No translation, or no such device: the raw states.
            "ha:fan": {"on": "on", "off": "off"},
            "ha:gone": {"on": "on", "off": "off"},
        }
    }


def test_http_reader_words_answers_every_requested_device_and_nothing_for_none(
    reader: TestClient, door_home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert reader.get("/internal/catalog", headers=AUTH).status_code == 200

    def forward(request: httpx.Request) -> httpx.Response:
        answer = reader.get(request.url.raw_path.decode(), headers=AUTH)
        return httpx.Response(answer.status_code, content=answer.content)

    transport = httpx.MockTransport(forward)
    monkeypatch.setattr(reader_module.httpx, "Client", partial(httpx.Client, transport=transport))
    http_reader = HttpReader("http://reader.internal", SERVICE_TOKEN)

    words = http_reader.words(["ha:door", "ha:gone"])

    assert {key: (value.on, value.off) for key, value in words.items()} == {
        "ha:door": ("Open", "Closed"),
        "ha:gone": ("on", "off"),
    }
    assert http_reader.words([]) == {}


@pytest.fixture
def power_home(home: Home) -> Home:
    """The home plus a power meter with a state class and long-term statistics: one hourly
    bucket now and one three hours later, and nothing between."""
    home.entities = [
        *ENTITIES,
        registry_entry("power", "sensor.desk_power", platform="demo", translation_key=None),
    ]
    home.server.states["sensor.desk_power"] = entity_state(
        "sensor.desk_power", "412", state_class="measurement", unit_of_measurement="W"
    )
    home.statistics = {
        "sensor.desk_power": [
            statistics_row(START, 10.5, 5, 20),
            statistics_row(START + timedelta(hours=3), 60, 1, 90),
        ]
    }
    return home


def statistics_frames(home: Home) -> list[Frame]:
    return [f for f in home.frames() if f["type"] == "recorder/statistics_during_period"]


def test_statistics_reads_the_requested_devices_buckets_in_one_request(
    reader: TestClient, power_home: Home
) -> None:
    assert reader.get("/internal/catalog", headers=AUTH).status_code == 200
    window = {"start": START.isoformat(), "end": (START + timedelta(days=7)).isoformat()}

    response = reader.get(
        "/internal/statistics",
        params=[("entity_id", "ha:power"), ("entity_id", "ha:gone"), *window.items()],
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    statistics = response.json()["statistics"]
    assert sorted(statistics) == ["ha:gone", "ha:power"]
    assert [(b["mean"], b["min"], b["max"]) for b in statistics["ha:power"]] == [
        (10.5, 5.0, 20.0),
        (60.0, 1.0, 90.0),
    ]
    # A device Home Assistant does not have has no buckets, and is not asked for.
    assert statistics["ha:gone"] == []
    [frame] = statistics_frames(power_home)
    assert frame["statistic_ids"] == ["sensor.desk_power"]
    assert frame["period"] == "hour"


def test_the_http_reader_returns_typed_buckets_for_every_requested_device(
    reader: TestClient, power_home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert reader.get("/internal/catalog", headers=AUTH).status_code == 200

    def forward(request: httpx.Request) -> httpx.Response:
        answer = reader.get(request.url.raw_path.decode(), headers=AUTH)
        return httpx.Response(answer.status_code, content=answer.content)

    transport = httpx.MockTransport(forward)
    monkeypatch.setattr(reader_module.httpx, "Client", partial(httpx.Client, transport=transport))
    http_reader = HttpReader("http://reader.internal", SERVICE_TOKEN)
    end = START + timedelta(days=7)

    buckets = http_reader.statistics(["ha:power", "ha:gone"], START, end)

    assert list(buckets) == ["ha:power", "ha:gone"]
    assert [
        (datetime.fromisoformat(b.start), datetime.fromisoformat(b.end), b.mean, b.min, b.max)
        for b in buckets["ha:power"]
    ] == [
        (START, START + timedelta(hours=1), 10.5, 5.0, 20.0),
        (START + timedelta(hours=3), START + timedelta(hours=4), 60.0, 1.0, 90.0),
    ]
    assert buckets["ha:gone"] == []
    assert http_reader.statistics([], START, end) == {}


def test_the_http_reader_raises_the_safe_code_when_the_reader_cannot_read_statistics(
    reader: TestClient, power_home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert reader.get("/internal/catalog", headers=AUTH).status_code == 200
    # One row without its max: Home Assistant's answer is invalid data.
    power_home.statistics = {"sensor.desk_power": [statistics_row(START, 1, 0, None)]}

    def forward(request: httpx.Request) -> httpx.Response:
        answer = reader.get(request.url.raw_path.decode(), headers=AUTH)
        return httpx.Response(answer.status_code, content=answer.content)

    transport = httpx.MockTransport(forward)
    monkeypatch.setattr(reader_module.httpx, "Client", partial(httpx.Client, transport=transport))
    http_reader = HttpReader("http://reader.internal", SERVICE_TOKEN)

    with pytest.raises(reader_module.ReaderUnavailable) as error:
        http_reader.statistics(["ha:power"], START, END)

    assert error.value.error == "invalid_data"


def test_an_unavailable_measurement_has_a_null_value_its_unit_and_the_translated_label(
    reader: TestClient, power_home: Home
) -> None:
    power_home.translations = {"component.sensor.entity_component._.state.unavailable": "Not here"}
    power_home.server.states["sensor.desk_power"] = entity_state(
        "sensor.desk_power", "unavailable", state_class="measurement", unit_of_measurement="W"
    )
    assert reader.get("/internal/catalog", headers=AUTH).status_code == 200

    response = reader.get("/internal/states", params={"entity_id": "ha:power"}, headers=AUTH)

    assert response.status_code == 200, response.text
    reading = response.json()["states"]["ha:power"]["reading"]
    assert without_times(reading) == {
        "device_kind": "measurement",
        "state": "unavailable",
        "availability": "unavailable",
        # Home Assistant's word for the state, from its translations.
        "label": "Not here",
        "unit": "W",
    }
    # The key is there, null, for a client that reads `value: number | null`.
    assert reading["value"] is None
