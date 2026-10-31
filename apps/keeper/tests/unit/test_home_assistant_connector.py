"""Home Assistant adapter against an in-memory websocket and a mocked REST transport."""

import json
import logging
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from support.home_assistant import (
    FakeClock,
    FakeSocket,
    Frame,
    StateServer,
    entity_state,
    hue_room,
    install_socket,
    light_state,
    realistic_device,
    realistic_entity,
    registry_entry,
    use_transport,
)

from home_keeper.home_assistant import (
    HISTORY_MAX_PAYLOAD,
    MAX_PAYLOAD,
    STATES_MAX_PAYLOAD,
    ConnectorError,
    HomeAssistantConnector,
    Session,
)
from home_keeper.models import LightState, SwitchState

type Install = Callable[..., FakeSocket]
type Connector = tuple[HomeAssistantConnector, Install, StateServer]

GROUP = "Groups and rooms are not recorded"
DISABLED = "Disabled in Home Assistant"
PRESENCE = "Presence is not recorded"
CATEGORY = "Settings and diagnostic entities are not recorded"
HIDDEN = "Hidden in Home Assistant"
NO_DEVICE_ENTITY = "Only entities of a device are recorded"
SETTING = "Settings and diagnostic switches are not recorded"
NO_DEVICE = "Switches without a device are not recorded"


@pytest.fixture
def connector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Connector]:
    """An adapter whose registries come from `install` and whose states from the server."""
    token = tmp_path / "token"
    token.write_text("secret-sentinel")
    server = StateServer()

    def install(
        entities: list[Frame],
        devices: Iterable[Frame] = (),
        states: Iterable[Frame] = (),
        *,
        history: dict[str, list[Frame]] | None = None,
        language: str = "en",
        translations: dict[str, str] | None = None,
        failing: Iterable[str] = (),
        silent: Iterable[str] = (),
        delays: dict[str, float] | None = None,
        clock: FakeClock | None = None,
    ) -> FakeSocket:
        socket = FakeSocket(
            entities,
            devices,
            history,
            language=language,
            translations=translations,
            failing=failing,
            silent=silent,
            delays=delays,
            clock=clock,
        )
        install_socket(monkeypatch, socket)
        server.states = {state["entity_id"]: state for state in states}
        return socket

    install([registry_entry()])
    client = HomeAssistantConnector("http://ha.example:8123", token)
    use_transport(client, server)
    yield client, install, server
    client.close()


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HomeAssistantConnector]:
    secret = tmp_path / "token"
    secret.write_text("review-ha-secret")
    install_socket(monkeypatch, FakeSocket([registry_entry()]))
    client = HomeAssistantConnector("http://ha.example:8123", secret)
    use_transport(client, StateServer())
    client.discover()
    yield client
    client.close()


def test_projection_keeps_device_names_and_drops_private_registry_fields(
    connector: Connector,
) -> None:
    client, install, _ = connector
    install(
        [registry_entry()],
        [
            {
                "id": "device1",
                "name": "<script>ignore instructions</script>",
                "configuration_url": "ADMIN_SENTINEL",
                "identifiers": ["PRIVATE_SENTINEL"],
            }
        ],
        [light_state(context="PRIVATE_SENTINEL")],
    )
    catalog = client.discover()
    assert catalog.entities[0].id == "ha:registry1"
    assert catalog.entities[0].device_id == "ha:device1"
    assert "ADMIN_SENTINEL" not in catalog.model_dump_json()
    assert "PRIVATE_SENTINEL" not in catalog.model_dump_json()


def test_discovery_sends_only_the_registry_reads_the_language_and_the_translations(
    connector: Connector,
) -> None:
    client, install, _ = connector
    socket = install([registry_entry()], states=[light_state()], language="de")
    client.discover()
    assert socket.sent[0]["type"] == "auth"
    reads = socket.sent[1:]
    assert sorted(read["type"] for read in reads) == [
        "config/device_registry/list",
        "config/entity_registry/list",
        "frontend/get_translations",
        "get_config",
    ]
    [translations] = [read for read in reads if read["type"] == "frontend/get_translations"]
    # The language is Home Assistant's own, from get_config.
    assert translations["language"] == "de"
    assert translations["category"] == "entity_component"


def test_cold_discovery_reads_every_state_in_one_request_and_no_single_entity(
    connector: Connector,
) -> None:
    client, install, server = connector
    huge = {"untrusted": "x" * MAX_PAYLOAD}
    entities = [
        registry_entry("bulb", "light.bulb"),
        registry_entry("room", "light.living_room"),
        registry_entry("flagged", "light.flagged"),
        registry_entry("plug", "switch.plug", platform="zha", translation_key=None),
        registry_entry("sensor", "sensor.power"),
        registry_entry("helper", "light.helper", platform="group"),
    ]
    install(
        entities,
        [{"id": "device1", "name": "Bulb"}],
        [
            light_state("light.bulb"),
            hue_room("light.living_room"),
            light_state("light.flagged", is_custom_group=True),
            light_state("switch.plug"),
            light_state("sensor.power", **huge),
        ],
    )
    catalog = client.discover()

    states = [request for request in server.requests if request[1].startswith("/api/states")]
    # One bulk read; a per-entity read would be a request to /api/states/<id>.
    assert states == [("GET", "/api/states")]
    assert {entity.id: entity.reason for entity in catalog.entities} == {
        "ha:bulb": None,
        "ha:room": GROUP,
        "ha:flagged": GROUP,
        "ha:plug": None,
        # A sensor on a device is a generic state device, so it is supported.
        "ha:sensor": None,
        "ha:helper": GROUP,
    }


def test_any_enabled_single_light_is_supported_and_groups_rooms_and_others_are_not(
    connector: Connector,
) -> None:
    client, install, _ = connector
    no_translation = {"translation_key": None}
    install(
        [
            registry_entry("hue", "light.bulb"),
            registry_entry("zha", "light.zha_bulb", platform="zha", **no_translation),
            # No state yet: Home Assistant answers 404, which is not a group.
            registry_entry("stateless", "light.stateless", platform="zha", **no_translation),
            registry_entry("not_group", "light.flagged", platform="hue"),
            registry_entry("helper", "light.helper_group", platform="group", **no_translation),
            registry_entry("room", "light.living_room", translation_key="hue_grouped_light"),
            registry_entry("members", "light.zha_group", platform="zha", **no_translation),
            registry_entry("deconz", "light.deconz_group", platform="deconz", **no_translation),
            registry_entry("disabled", "light.disabled", platform="zha", disabled_by="user"),
            registry_entry("sensor", "sensor.x", platform="zha", **no_translation),
        ],
        [{"id": "device1", "name": "Bulb"}],
        states=[
            light_state("light.bulb"),
            light_state("light.zha_bulb", color_mode="brightness"),
            light_state("light.flagged", is_hue_group=False),
            light_state("light.helper_group"),
            hue_room("light.living_room"),
            light_state("light.zha_group", entity_id=["light.zha_bulb", "light.stateless"]),
            light_state("light.deconz_group", is_deconz_group=True),
            light_state("sensor.x"),
        ],
    )
    catalog = client.discover()
    assert {
        entity.id: (entity.supported, entity.reason, entity.model_dump().get("device_kind"))
        for entity in catalog.entities
    } == {
        "ha:hue": (True, None, "light"),
        "ha:zha": (True, None, "light"),
        "ha:stateless": (True, None, "light"),
        "ha:not_group": (True, None, "light"),
        "ha:helper": (False, GROUP, "light"),
        "ha:room": (False, GROUP, "light"),
        "ha:members": (False, GROUP, "light"),
        "ha:deconz": (False, GROUP, "light"),
        "ha:disabled": (False, DISABLED, "light"),
        "ha:sensor": (True, None, "state"),
    }


def test_a_plug_or_switch_on_a_device_is_supported_and_settings_groups_and_loose_ones_are_not(
    connector: Connector,
) -> None:
    client, install, _ = connector
    zha = {"platform": "zha", "translation_key": None}
    install(
        [
            registry_entry("plug", "switch.plug", **zha),
            registry_entry("config", "switch.child_lock", entity_category="config", **zha),
            registry_entry("diagnostic", "switch.identify", entity_category="diagnostic", **zha),
            registry_entry("loose", "switch.template", device_id=None, **zha),
            registry_entry("dangling", "switch.dangling", device_id="gone", **zha),
            registry_entry("helper", "switch.helper_group", platform="group"),
            registry_entry("members", "switch.all_plugs", **zha),
            registry_entry("flagged", "switch.flagged_group", **zha),
            registry_entry("disabled", "switch.disabled", disabled_by="user", **zha),
            # Disabled is checked before the switch-only device rule.
            registry_entry(
                "disabled_loose", "switch.old", disabled_by="user", device_id=None, **zha
            ),
            # Only a switch that passes the other rules has its state read for group markers.
            registry_entry(
                "group_setting", "switch.group_setting", entity_category="config", **zha
            ),
            registry_entry("sensor", "sensor.power", **zha),
        ],
        [{"id": "device1", "name": "Smart plug"}],
        states=[
            light_state("switch.plug", brightness=120),
            light_state("switch.all_plugs", entity_id=["switch.plug", "switch.template"]),
            light_state("switch.flagged_group", is_zha_group=True),
            light_state("switch.group_setting", entity_id=["switch.plug"]),
            light_state("sensor.power"),
        ],
    )
    catalog = client.discover()
    assert {
        entity.id: (entity.supported, entity.reason, entity.model_dump().get("device_kind"))
        for entity in catalog.entities
    } == {
        "ha:plug": (True, None, "switch"),
        "ha:config": (False, SETTING, "switch"),
        "ha:diagnostic": (False, SETTING, "switch"),
        "ha:loose": (False, NO_DEVICE, "switch"),
        "ha:dangling": (False, NO_DEVICE, "switch"),
        "ha:helper": (False, GROUP, "switch"),
        "ha:members": (False, GROUP, "switch"),
        "ha:flagged": (False, GROUP, "switch"),
        "ha:disabled": (False, DISABLED, "switch"),
        "ha:disabled_loose": (False, DISABLED, "switch"),
        "ha:group_setting": (False, SETTING, "switch"),
        "ha:sensor": (True, None, "state"),
    }


def test_a_switch_reads_its_state_without_light_attributes(
    connector: Connector,
) -> None:
    client, _, _ = connector
    use_transport(
        client,
        lambda request: httpx.Response(
            200,
            json=[
                {
                    "entity_id": "switch.plug",
                    "state": "on",
                    "last_changed": "2026-10-01T10:00:00Z",
                    "attributes": {"brightness": 120, "color_mode": "brightness"},
                }
            ],
        ),
    )
    state = client.read_states(["switch.plug"])["switch.plug"]
    assert isinstance(state, SwitchState)
    reading = state.model_dump(exclude_none=True)
    assert reading["device_kind"] == "switch"
    assert reading["state"] == "on"
    assert reading["availability"] == "available"
    assert "brightness" not in reading
    assert "color_mode" not in reading


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={}),
        # None: Home Assistant cannot be reached.
        None,
    ],
    ids=["server_error", "unreachable"],
)
def test_a_failed_bulk_states_read_fails_discovery_and_is_retried_next_pass(
    connector: Connector, response: httpx.Response | None
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if response is None:
            raise httpx.ConnectError("refused", request=request)
        return response

    client, install, server = connector
    entities = [registry_entry(), registry_entry("desk", "light.desk")]
    client.discover()
    install(entities, states=[hue_room("light.desk")])
    use_transport(client, reply)
    with pytest.raises(ConnectorError) as failure:
        client.discover()
    assert failure.value.code == "discovery"
    assert "secret-sentinel" not in str(failure.value)

    # The next pass reads /api/states again; an entity missing from the list is not a group.
    server.requests.clear()
    use_transport(client, server)
    install(entities, states=[hue_room("light.desk")])
    catalog = client.discover()
    assert [path for _, path in server.requests if path.startswith("/api/states")] == [
        "/api/states"
    ]
    assert {entity.id: entity.reason for entity in catalog.entities} == {
        "ha:registry1": None,
        "ha:desk": GROUP,
    }


def test_states_are_read_at_once_keeping_brightness_and_dropping_private_attributes(
    connector: Connector,
) -> None:
    client, _, _ = connector
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "entity_id": entity_id,
                    "state": "on",
                    "last_changed": "2026-10-01T10:00:00Z",
                    "attributes": {"brightness": 120, "context": "PRIVATE_SENTINEL"},
                }
                for entity_id in ("light.bulb", "light.desk", "sensor.power")
            ],
        )

    use_transport(client, handle)
    states = client.read_states(["light.bulb", "light.desk", "light.gone"])
    assert set(states) == {"light.bulb", "light.desk", "light.gone"}
    assert states["light.gone"] is None
    for entity_id in ("light.bulb", "light.desk"):
        state = states[entity_id]
        assert isinstance(state, LightState)
        assert state.brightness == 120
        assert "PRIVATE_SENTINEL" not in state.model_dump_json()
    assert [(call.method, call.url.path) for call in calls] == [("GET", "/api/states")]


def test_a_malformed_entity_id_is_refused_without_a_request(connector: Connector) -> None:
    client, _, _ = connector
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"entity_id": "light.bulb", "state": "on"})

    use_transport(client, handle)
    malformed = (
        "light.bulb/../../api/services",
        "light.bulb?x=1",
        "climate.hall/../../api/services",
        "Sensor.power",
        "sensor",
        "sensor.",
        ".power",
    )
    for entity_id in malformed:
        with pytest.raises(ConnectorError) as error:
            client.read_states(["light.bulb", entity_id])
        assert error.value.code == "configuration"
    assert calls == []


def test_failed_discovery_authentication_is_redacted(
    connector: Connector,
) -> None:
    client, install, _ = connector
    client.discover()
    socket = install([])
    socket.messages[1] = {"type": "auth_invalid", "message": "secret-sentinel"}
    with pytest.raises(ConnectorError) as failure:
        client.discover()
    assert failure.value.code == "authentication"
    assert "secret-sentinel" not in str(failure.value)


@pytest.mark.parametrize(
    "origin",
    [
        "http://user:secret@host",
        "http://host/path",
        "file:///tmp",
        "http://host?x=y",
        "http://host#x",
        "http://host\\path",
        "http://host:",
        "http://host\x00secret",
    ],
)
def test_invalid_origin_safe(origin: str, tmp_path: Path) -> None:
    with pytest.raises(ConnectorError) as error:
        HomeAssistantConnector(origin, tmp_path / "missing")
    assert "secret" not in str(error.value)


def test_a_missing_entity_reads_as_none_until_it_returns(
    connector: Connector,
) -> None:
    client, _, _ = connector
    states: list[Frame] = []
    use_transport(client, lambda _request: httpx.Response(200, json=states))
    assert client.read_states(["light.bulb"]) == {"light.bulb": None}
    states.append({"entity_id": "light.bulb", "state": "on", "attributes": {}})
    state = client.read_states(["light.bulb"])["light.bulb"]
    assert state is not None
    assert state.state == "on"


def test_names_redact_credentials_and_write_helper_rejected(
    connector: Connector,
) -> None:
    client, install, _ = connector
    client._redactions = ("secret-sentinel", "internal-sentinel")  # pyright: ignore[reportPrivateUsage] -- inject redactions
    socket = install([registry_entry(name="secret-sentinel internal-sentinel")])
    assert client.discover().entities[0].name == "[redacted] [redacted]"
    count = len(socket.sent)
    for command in ("call_service", "fire_event", "config/device_registry/update"):
        with pytest.raises(ConnectorError):
            Session(client, socket, MAX_PAYLOAD).request(command, failure="discovery")
    assert len(socket.sent) == count


def test_payload_limit_and_contradictory_state_rejected(
    connector: Connector,
) -> None:
    client, _, _ = connector
    for response in (
        httpx.Response(200, content=b"x" * (STATES_MAX_PAYLOAD + 1)),
        httpx.Response(200, json={"entity_id": "light.bulb", "state": "on"}),
        httpx.Response(200, json=["light.bulb"]),
        httpx.Response(200, json=[{"entity_id": "light.bulb", "state": "secret-sentinel"}]),
    ):
        use_transport(client, lambda _request, response=response: response)
        with pytest.raises(ConnectorError) as error:
            client.read_states(["light.bulb"])
        assert error.value.code == "invalid_data"
        assert "secret-sentinel" not in str(error.value)


def test_slow_stream_total_time_is_bounded(
    connector: Connector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = connector

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'[{"entity_id": "light.bulb",'
            yield b'"state": "on"}]'

    clock = iter([0, 1, 6])
    monkeypatch.setattr("home_keeper.home_assistant.time.monotonic", lambda: next(clock))
    use_transport(client, lambda _request: httpx.Response(200, stream=Stream()))
    with pytest.raises(ConnectorError) as error:
        client.read_states(["light.bulb"])
    assert error.value.code == "timeout"


@pytest.mark.parametrize("kind", ["oversized", "unsupported_state", "redirect"])
def test_an_invalid_state_response_is_safe(
    adapter: HomeAssistantConnector, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    payload: dict[str, object] = {"entity_id": "light.bulb", "state": "on", "attributes": {}}
    if kind == "oversized":
        payload["untrusted"] = "review-ha-secret" + "x" * STATES_MAX_PAYLOAD
    if kind == "unsupported_state":
        payload["state"] = "review-ha-secret"
    calls: list[httpx.URL] = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(
            302 if kind == "redirect" else 200,
            json=[payload],
            headers={"Location": "http://admin.example/write"},
        )

    use_transport(adapter, response)
    with caplog.at_level(logging.DEBUG), pytest.raises(ConnectorError) as error:
        adapter.read_states(["light.bulb"])
    assert len(calls) == 1
    assert calls[0].path == "/api/states"
    assert "review-ha-secret" not in str(error.value)
    assert "review-ha-secret" not in caplog.text


def test_a_state_read_timeout_does_not_leak(
    adapter: HomeAssistantConnector, caplog: pytest.LogCaptureFixture
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("review-ha-secret", request=request)

    use_transport(adapter, timeout)
    with caplog.at_level(logging.DEBUG), pytest.raises(ConnectorError) as error:
        adapter.read_states(["light.bulb"])
    assert error.value.code == "timeout"
    assert "review-ha-secret" not in str(error.value)
    assert "review-ha-secret" not in caplog.text


def test_registry_payload_limit_before_projection(
    adapter: HomeAssistantConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket = FakeSocket([registry_entry()])
    socket.messages[2]["result"][0]["unused"] = "x" * MAX_PAYLOAD
    install_socket(monkeypatch, socket)
    with pytest.raises(ConnectorError) as error:
        adapter.discover()
    assert error.value.code == "invalid_data"
    sent = [frame["type"] for frame in socket.sent]
    assert "config/entity_registry/list" in sent
    # The oversized registry list ended discovery before the device list was asked for.
    assert "config/device_registry/list" not in sent


def registry_of(entities: int, devices: int) -> tuple[list[Frame], list[Frame]]:
    return (
        [realistic_entity(number) for number in range(entities)],
        [realistic_device(number) for number in range(devices)],
    )


@pytest.mark.parametrize(("entities", "devices"), [(5000, 3), (3, 5000), (501, 501)])
def test_a_registry_of_up_to_5000_entity_or_device_rows_is_discovered(
    connector: Connector, entities: int, devices: int
) -> None:
    client, install, _ = connector
    entity_rows, device_rows = registry_of(entities, devices)
    # Real registry rows are 400 to 600 bytes, so 5000 of them weigh over 2.5 MiB. Rows
    # without history keep the test honest about message size.
    heaviest = max(len(json.dumps(entity_rows)), len(json.dumps(device_rows)))
    if max(entities, devices) == 5000:
        assert heaviest > 2.5 * 1024 * 1024
    install(entity_rows, device_rows)

    catalog = client.discover()

    assert len(catalog.entities) == entities


@pytest.mark.parametrize(("entities", "devices"), [(5001, 3), (3, 5001)])
def test_5001_entity_rows_or_5001_device_rows_fail_with_invalid_data(
    connector: Connector, entities: int, devices: int
) -> None:
    client, install, _ = connector
    entity_rows, device_rows = registry_of(entities, devices)
    install(entity_rows, device_rows)

    with pytest.raises(ConnectorError) as error:
        client.discover()

    assert error.value.code == "invalid_data"


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b'{"entity_id": "light.bulb"}',
        b'["not a state"]',
        b'[{"entity_id": "light.bulb", "attributes": []}]',
    ],
    ids=["not_json", "not_a_list", "entry_not_an_object_state", "attributes_not_an_object"],
)
def test_a_malformed_bulk_states_body_fails_discovery_with_invalid_data(
    connector: Connector, body: bytes
) -> None:
    client, install, _ = connector
    install([registry_entry()])
    use_transport(client, lambda _request: httpx.Response(200, content=body))

    with pytest.raises(ConnectorError) as failure:
        client.discover()

    assert failure.value.code == "invalid_data"


def test_an_entity_of_any_other_domain_on_a_device_is_a_state_device_unless_a_rule_excludes_it(
    connector: Connector,
) -> None:
    client, install, _ = connector
    demo = {"platform": "demo", "translation_key": None}
    install(
        [
            registry_entry("climate", "climate.hall", **demo),
            registry_entry("cover", "cover.garage", **demo),
            registry_entry("text", "sensor.note", **demo),
            registry_entry("diagnostic", "sensor.rssi", entity_category="diagnostic", **demo),
            registry_entry("config", "number.offset", entity_category="config", **demo),
            registry_entry("hidden", "climate.spare", hidden_by="user", **demo),
            registry_entry("integration_hidden", "climate.old", hidden_by="integration", **demo),
            registry_entry("loose", "sensor.template", device_id=None, **demo),
            registry_entry("dangling", "sensor.dangling", device_id="gone", **demo),
            registry_entry("person", "person.ali", **demo),
            registry_entry("tracker", "device_tracker.phone", **demo),
            registry_entry("disabled", "climate.off", disabled_by="user", **demo),
            registry_entry("helper", "climate.helper", platform="group"),
            registry_entry("members", "climate.zones", **demo),
            registry_entry("flagged", "climate.flagged", **demo),
            # Each rule's reason comes first in the fixed order: disabled, presence,
            # category, hidden, device, group.
            registry_entry("disabled_person", "person.gone", disabled_by="user", **demo),
            registry_entry("person_category", "person.setting", entity_category="config", **demo),
            registry_entry(
                "category_hidden", "sensor.both", entity_category="config", hidden_by="user", **demo
            ),
            registry_entry(
                "hidden_loose", "sensor.both_two", hidden_by="user", device_id=None, **demo
            ),
            registry_entry("loose_group", "climate.loose_group", device_id=None, platform="group"),
        ],
        [{"id": "device1", "name": "Hall"}],
        states=[
            entity_state("climate.hall", "heat"),
            entity_state("cover.garage", "closed"),
            entity_state("sensor.note", "Back in five minutes"),
            entity_state("climate.zones", "heat", entity_id=["climate.hall", "climate.spare"]),
            entity_state("climate.flagged", "heat", is_demo_group=True),
        ],
    )
    catalog = client.discover()
    by_id = {entity.id: entity for entity in catalog.entities}
    assert {key: (entity.supported, entity.reason) for key, entity in by_id.items()} == {
        "ha:climate": (True, None),
        "ha:cover": (True, None),
        "ha:text": (True, None),
        "ha:diagnostic": (False, CATEGORY),
        "ha:config": (False, CATEGORY),
        "ha:hidden": (False, HIDDEN),
        "ha:integration_hidden": (False, HIDDEN),
        "ha:loose": (False, NO_DEVICE_ENTITY),
        "ha:dangling": (False, NO_DEVICE_ENTITY),
        "ha:person": (False, PRESENCE),
        "ha:tracker": (False, PRESENCE),
        "ha:disabled": (False, DISABLED),
        "ha:helper": (False, GROUP),
        "ha:members": (False, GROUP),
        "ha:flagged": (False, GROUP),
        "ha:disabled_person": (False, DISABLED),
        "ha:person_category": (False, PRESENCE),
        "ha:category_hidden": (False, CATEGORY),
        "ha:hidden_loose": (False, HIDDEN),
        "ha:loose_group": (False, NO_DEVICE_ENTITY),
    }
    for supported in ("climate", "cover", "text"):
        assert by_id[f"ha:{supported}"].model_dump()["device_kind"] == "state"


def test_lights_and_switches_keep_their_own_rules_beside_state_devices(
    connector: Connector,
) -> None:
    client, install, _ = connector
    zha = {"platform": "zha", "translation_key": None}
    install(
        [
            # A light needs no device; a switch does. A state device needs one too.
            registry_entry("light", "light.loose", device_id=None, **zha),
            registry_entry("switch", "switch.loose", device_id=None, **zha),
            registry_entry("climate", "climate.loose", device_id=None, **zha),
            # A light with an entity category and a hidden light stay supported as today.
            registry_entry("setting_light", "light.setting", entity_category="config", **zha),
            registry_entry("hidden_light", "light.hidden", hidden_by="user", **zha),
            registry_entry("hidden_switch", "switch.hidden", hidden_by="user", **zha),
        ],
        [{"id": "device1", "name": "Hall"}],
        states=[
            light_state("light.loose"),
            light_state("switch.loose"),
            light_state("light.setting"),
            light_state("light.hidden"),
            light_state("switch.hidden"),
        ],
    )
    catalog = client.discover()
    assert {
        entity.id: (entity.supported, entity.reason, entity.model_dump().get("device_kind"))
        for entity in catalog.entities
    } == {
        "ha:light": (True, None, "light"),
        "ha:switch": (False, NO_DEVICE, "switch"),
        "ha:climate": (False, NO_DEVICE_ENTITY, "state"),
        "ha:setting_light": (True, None, "light"),
        "ha:hidden_light": (True, None, "light"),
        "ha:hidden_switch": (True, None, "switch"),
    }


TRANSLATIONS = {
    "component.climate.entity_component._.state.heat": "Heat",
    "component.climate.entity_component._.state.off": "Off",
    "component.light.entity_component._.state.on": "On",
    "component.binary_sensor.entity_component._.state.on": "Detected",
    "component.binary_sensor.entity_component.door.state.on": "Open",
}
TRANSLATED_ENTITIES = [
    registry_entry("climate", "climate.hall", platform="demo", translation_key=None),
    registry_entry("untranslated", "climate.attic", platform="demo", translation_key=None),
    registry_entry("door", "binary_sensor.front", platform="demo", translation_key=None),
    registry_entry("motion", "binary_sensor.hall", platform="demo", translation_key=None),
    registry_entry("light", "light.desk", platform="demo", translation_key=None),
]
TRANSLATED_STATES = [
    entity_state("climate.hall", "heat", temperature=21),
    entity_state("climate.attic", "frost_guard"),
    entity_state("binary_sensor.front", "on", device_class="door"),
    entity_state("binary_sensor.hall", "on", device_class="motion"),
    entity_state("light.desk", "on"),
]
TRANSLATED_IDS = [state["entity_id"] for state in TRANSLATED_STATES]


def labels(client: HomeAssistantConnector) -> dict[str, str]:
    readings = client.read_states(TRANSLATED_IDS)
    return {
        entity_id: reading.model_dump()["label"]
        for entity_id, reading in readings.items()
        if reading is not None
    }


def test_a_reading_is_labeled_with_homeassistants_translation_of_its_state(
    connector: Connector,
) -> None:
    client, install, _ = connector
    socket = install(
        TRANSLATED_ENTITIES,
        [{"id": "device1", "name": "Hall"}],
        TRANSLATED_STATES,
        translations=TRANSLATIONS,
    )
    client.discover()
    sent = len(socket.sent)

    assert labels(client) == {
        "climate.hall": "Heat",
        # No translation for this state: the label is the raw state.
        "climate.attic": "frost_guard",
        # The device class's translation wins over the domain's.
        "binary_sensor.front": "Open",
        # No `motion` translation: the domain's own comes next.
        "binary_sensor.hall": "Detected",
        # Lights and switches carry a label too.
        "light.desk": "On",
    }
    # The translations were read once with the catalog, not again for each state read.
    assert len(socket.sent) == sent


@pytest.mark.parametrize("failing", ["frontend/get_translations", "get_config"])
def test_a_failed_translations_or_language_read_does_not_fail_discovery(
    connector: Connector, failing: str
) -> None:
    client, install, _ = connector
    socket = install(
        TRANSLATED_ENTITIES,
        [{"id": "device1", "name": "Hall"}],
        TRANSLATED_STATES,
        translations=TRANSLATIONS,
        failing=[failing],
    )

    catalog = client.discover()

    assert all(entity.supported for entity in catalog.entities)
    [request] = [r for r in socket.sent if r["type"] == "frontend/get_translations"]
    assert request["language"] == "en"
    if failing == "frontend/get_translations":
        # Every label falls back to the raw state.
        assert labels(client) == {
            "climate.hall": "heat",
            "climate.attic": "frost_guard",
            "binary_sensor.front": "on",
            "binary_sensor.hall": "on",
            "light.desk": "on",
        }
    else:
        # Home Assistant's language is unknown, so English is asked for.
        assert labels(client)["climate.hall"] == "Heat"


def test_a_state_device_reading_keeps_its_state_and_times_and_no_attributes(
    connector: Connector,
) -> None:
    client, install, _ = connector
    install(
        [registry_entry("climate", "climate.hall", platform="demo", translation_key=None)],
        [{"id": "device1", "name": "Hall"}],
        [
            entity_state(
                "climate.hall",
                "heat",
                temperature=21,
                hvac_modes=["heat"],
                secret="PRIVATE_SENTINEL",
            )
        ],
    )
    client.discover()

    reading = client.read_states(["climate.hall"])["climate.hall"]

    assert reading is not None
    encoded = reading.model_dump(exclude_none=True)
    assert encoded["device_kind"] == "state"
    assert encoded["state"] == "heat"
    assert encoded["availability"] == "available"
    assert set(encoded) <= {
        "device_kind",
        "state",
        "availability",
        "label",
        "last_changed",
        "last_updated",
        "last_reported",
    }
    assert "PRIVATE_SENTINEL" not in reading.model_dump_json()


@pytest.mark.parametrize("word", ["unavailable", "unknown"])
def test_a_state_device_that_reports_unavailable_or_unknown_is_not_available(
    connector: Connector, word: str
) -> None:
    client, install, _ = connector
    install([], states=[entity_state("climate.hall", word)])

    reading = client.read_states(["climate.hall"])["climate.hall"]

    assert reading is not None
    assert reading.model_dump()["availability"] == word


def test_a_state_of_255_characters_is_read_and_one_of_256_is_invalid_data(
    connector: Connector,
) -> None:
    client, install, server = connector
    install([], states=[entity_state("sensor.note", "x" * 255)])
    reading = client.read_states(["sensor.note"])["sensor.note"]
    assert reading is not None
    assert reading.state == "x" * 255

    server.states = {"sensor.note": entity_state("sensor.note", "y" * 256)}
    with pytest.raises(ConnectorError) as error:
        client.read_states(["sensor.note"])
    assert error.value.code == "invalid_data"
    assert "y" * 256 not in str(error.value)


def test_history_of_a_state_device_is_read_without_attributes_and_a_light_keeps_them(
    connector: Connector,
) -> None:
    client, install, _ = connector
    start = datetime(2026, 10, 13, 9, tzinfo=UTC)
    end = datetime(2026, 10, 13, 10, tzinfo=UTC)
    at = start.timestamp()
    socket = install(
        [],
        history={
            "climate.hall": [
                {"s": "heat", "lu": at},
                {"s": "off", "lu": at + 60},
            ],
            "light.desk": [{"s": "on", "a": {"brightness": 120}, "lu": at}],
        },
    )

    history = client.history(["climate.hall", "light.desk"], start, end)

    assert [reading.state for reading in history["climate.hall"]] == ["heat", "off"]
    assert {reading.device_kind for reading in history["climate.hall"]} == {"state"}
    assert history["light.desk"][0].model_dump()["brightness"] == 120
    requests = [r for r in socket.sent if r["type"] == "history/history_during_period"]
    # One request may not mix the two, so Home Assistant may be asked twice.
    for request in requests:
        assert request["no_attributes"] is ("climate.hall" in request["entity_ids"])
        assert not {"climate.hall", "light.desk"} <= set(request["entity_ids"])


def test_busy_sensors_each_get_their_history_and_the_light_keeps_its_attributes(
    connector: Connector,
) -> None:
    client, install, _ = connector
    start = datetime(2026, 10, 13, tzinfo=UTC)
    end = start + timedelta(days=1)
    rows = 43_000
    # Home Assistant sends Unix seconds as floats with microseconds.
    busy = {
        f"sensor.busy_{number}": [
            {"s": f"{20 + index % 7}.5", "lu": start.timestamp() + index * 2 + 0.123456}
            for index in range(rows)
        ]
        for number in range(10)
    }
    light: list[Frame] = [
        {"s": "on", "a": {"brightness": 120}, "lu": start.timestamp() + 1.654321},
        {"s": "off", "a": {}, "lu": start.timestamp() + 90.5},
    ]
    # Together the sensors outgrow one answer, so one request for all of them cannot work.
    assert len(json.dumps(busy)) > HISTORY_MAX_PAYLOAD
    assert all(len(json.dumps(one)) < HISTORY_MAX_PAYLOAD for one in busy.values())
    socket = install([], history={**busy, "light.desk": light})

    history = client.history([*busy, "light.desk"], start, end)

    assert {entity_id: len(readings) for entity_id, readings in history.items()} == {
        **dict.fromkeys(busy, rows),
        "light.desk": 2,
    }
    assert history["light.desk"][0].model_dump()["brightness"] == 120
    requests = [r for r in socket.sent if r["type"] == "history/history_during_period"]
    for request in requests:
        sensors = [i for i in request["entity_ids"] if i.startswith("sensor.")]
        if sensors:
            # Each non-light device is read on its own, without attributes.
            assert request["entity_ids"] == sensors
            assert len(sensors) == 1
            assert request["no_attributes"] is True
        else:
            assert request["entity_ids"] == ["light.desk"]
            assert request["no_attributes"] is False
    assert len(requests) == 11


def test_discovery_gives_up_on_unanswered_language_and_translation_reads_within_a_budget(
    connector: Connector,
) -> None:
    client, install, _ = connector
    socket = install(
        [
            registry_entry("climate", "climate.hall", platform="demo", translation_key=None),
            registry_entry("light", "light.desk", platform="demo", translation_key=None),
        ],
        [{"id": "device1", "name": "Hall"}],
        [entity_state("climate.hall", "heat"), entity_state("light.desk", "on")],
        translations=TRANSLATIONS,
        silent=["get_config", "frontend/get_translations"],
    )

    catalog = client.discover()

    # The registry answers that follow, or come before, are not mistaken for the late ones.
    assert {entity.id: entity.supported for entity in catalog.entities} == {
        "ha:climate": True,
        "ha:light": True,
    }
    assert catalog.entities[0].device_name == "Hall"
    # Every wait on Home Assistant's silence is bounded, and together they stay short.
    assert socket.waits
    assert all(wait is not None for wait in socket.waits)
    assert sum(wait for wait in socket.waits if wait is not None) <= 2.5
    # Without translations the label is the raw state.
    readings = client.read_states(["climate.hall", "light.desk"])
    assert [reading.model_dump()["label"] for reading in readings.values() if reading] == [
        "heat",
        "on",
    ]


ON_OFF_DOMAINS = ["fan", "binary_sensor", "siren", "humidifier", "remote"]


@pytest.mark.parametrize("domain", ON_OFF_DOMAINS)
def test_a_fan_sensor_siren_humidifier_or_remote_on_a_device_is_a_supported_on_off_device(
    connector: Connector, domain: str
) -> None:
    client, install, _ = connector
    demo = {"platform": "demo", "translation_key": None}
    install(
        [
            registry_entry("ok", f"{domain}.hall", **demo),
            registry_entry("config", f"{domain}.setting", entity_category="config", **demo),
            registry_entry("hidden", f"{domain}.spare", hidden_by="user", **demo),
            registry_entry("loose", f"{domain}.loose", device_id=None, **demo),
            registry_entry("disabled", f"{domain}.off", disabled_by="user", **demo),
            registry_entry("helper", f"{domain}.helper", platform="group"),
            registry_entry("members", f"{domain}.zones", **demo),
        ],
        [{"id": "device1", "name": "Hall"}],
        states=[
            entity_state(f"{domain}.hall", "on"),
            entity_state(f"{domain}.zones", "on", entity_id=[f"{domain}.hall"]),
        ],
    )

    catalog = client.discover()

    by_id = {entity.id: entity for entity in catalog.entities}
    # The registry rule is the one of state devices.
    assert {key: (entity.supported, entity.reason) for key, entity in by_id.items()} == {
        "ha:ok": (True, None),
        "ha:config": (False, CATEGORY),
        "ha:hidden": (False, HIDDEN),
        "ha:loose": (False, NO_DEVICE_ENTITY),
        "ha:disabled": (False, DISABLED),
        "ha:helper": (False, GROUP),
        "ha:members": (False, GROUP),
    }
    # The kind is set on unsupported entities of the domain too.
    assert {entity.model_dump()["device_kind"] for entity in by_id.values()} == {"on_off"}


DOOR_TRANSLATIONS = {
    "component.binary_sensor.entity_component.door.state.on": "Open",
    "component.binary_sensor.entity_component.door.state.off": "Closed",
    "component.binary_sensor.entity_component._.state.on": "Detected",
    "component.fan.entity_component._.state.on": "On",
}


def test_an_on_off_reading_has_no_attributes_and_is_labeled_by_its_device_class(
    connector: Connector,
) -> None:
    client, install, _ = connector
    demo = {"platform": "demo", "translation_key": None}
    install(
        [
            registry_entry("door", "binary_sensor.front", **demo),
            registry_entry("fan", "fan.ceiling", **demo),
        ],
        [{"id": "device1", "name": "Hall"}],
        [
            entity_state(
                "binary_sensor.front", "on", device_class="door", secret="PRIVATE_SENTINEL"
            ),
            entity_state("binary_sensor.back", "off", device_class="door"),
            entity_state("fan.ceiling", "on", percentage=50, preset_mode="PRIVATE_SENTINEL"),
        ],
        translations=DOOR_TRANSLATIONS,
    )
    client.discover()

    readings = client.read_states(["binary_sensor.front", "fan.ceiling"])

    door, fan = readings["binary_sensor.front"], readings["fan.ceiling"]
    assert door is not None
    assert fan is not None
    for reading in (door, fan):
        encoded = reading.model_dump(exclude_none=True)
        assert encoded["device_kind"] == "on_off"
        assert encoded["state"] == "on"
        assert encoded["availability"] == "available"
        assert set(encoded) <= {
            "device_kind",
            "state",
            "availability",
            "label",
            "last_changed",
            "last_updated",
            "last_reported",
        }
        assert "PRIVATE_SENTINEL" not in reading.model_dump_json()
    assert door.model_dump()["label"] == "Open"
    assert fan.model_dump()["label"] == "On"


@pytest.mark.parametrize("word", ["unavailable", "unknown"])
def test_an_on_off_device_that_reports_unavailable_or_unknown_is_not_available(
    connector: Connector, word: str
) -> None:
    client, install, _ = connector
    install([], states=[entity_state("binary_sensor.front", word)])

    reading = client.read_states(["binary_sensor.front"])["binary_sensor.front"]

    assert reading is not None
    assert reading.model_dump()["device_kind"] == "on_off"
    assert reading.model_dump()["availability"] == word


def test_history_of_on_off_devices_is_read_one_device_per_request_without_attributes(
    connector: Connector,
) -> None:
    client, install, _ = connector
    start = datetime(2026, 10, 13, 9, tzinfo=UTC)
    end = datetime(2026, 10, 13, 10, tzinfo=UTC)
    at = start.timestamp()
    install(
        [registry_entry("door", "binary_sensor.front", platform="demo", translation_key=None)],
        [{"id": "device1", "name": "Hall"}],
        [entity_state("binary_sensor.front", "on", device_class="door")],
        translations=DOOR_TRANSLATIONS,
    )
    client.discover()
    # A fresh connection for the history read; the labels learned in discovery stay.
    socket = install(
        [],
        history={
            "binary_sensor.front": [{"s": "off", "lu": at}, {"s": "on", "lu": at + 60}],
            "fan.ceiling": [{"s": "on", "lu": at}],
            "light.desk": [{"s": "on", "a": {"brightness": 120}, "lu": at}],
        },
    )

    history = client.history(["binary_sensor.front", "fan.ceiling", "light.desk"], start, end)

    door = history["binary_sensor.front"]
    assert [(row.model_dump()["state"], row.model_dump()["label"]) for row in door] == [
        ("off", "Closed"),
        ("on", "Open"),
    ]
    assert {row.model_dump()["device_kind"] for row in door + history["fan.ceiling"]} == {"on_off"}
    assert history["light.desk"][0].model_dump()["brightness"] == 120
    requests = [r for r in socket.sent if r["type"] == "history/history_during_period"]
    assert sorted(r["entity_ids"] for r in requests) == [
        ["binary_sensor.front"],
        ["fan.ceiling"],
        ["light.desk"],
    ]
    for request in requests:
        assert request["no_attributes"] is (request["entity_ids"] != ["light.desk"])


def test_history_of_slow_devices_gives_up_with_a_timeout_within_8_seconds_in_all(
    connector: Connector, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, install, _ = connector
    clock = FakeClock()
    monkeypatch.setattr("home_keeper.home_assistant.time", clock)
    devices = [f"sensor.slow_{number}" for number in range(10)]
    # Each device answers in 3 s: alone fast enough, together far too slow.
    install(
        [],
        history={device: [{"s": "1", "lu": 1.0}] for device in devices},
        delays={"history/history_during_period": 3.0},
        clock=clock,
    )
    started = clock.monotonic()

    with pytest.raises(ConnectorError) as error:
        client.history(
            devices, datetime(2026, 10, 13, 9, tzinfo=UTC), datetime(2026, 10, 13, 10, tzinfo=UTC)
        )

    assert error.value.code == "timeout"
    assert clock.monotonic() - started <= 8


def test_history_answers_that_together_pass_64_mib_are_invalid_data(
    connector: Connector,
) -> None:
    client, install, _ = connector
    # About 13.5 MiB of rows per device: each answer is under the 16 MiB cap, five are over 64.
    row = {"s": "x" * 250, "lu": 1_760_000_000.0}
    rows = [row] * (13_500_000 // len(json.dumps(row)))
    devices = [f"sensor.busy_{number}" for number in range(5)]
    install([], history=dict.fromkeys(devices, rows))
    assert len(json.dumps({"x": rows})) < HISTORY_MAX_PAYLOAD
    assert 5 * len(json.dumps(rows)) > 64 * 1024 * 1024

    with pytest.raises(ConnectorError) as error:
        client.history(
            devices, datetime(2026, 10, 13, tzinfo=UTC), datetime(2026, 10, 14, tzinfo=UTC)
        )

    assert error.value.code == "invalid_data"


def test_discovery_waits_only_the_budget_for_silent_translations_when_the_language_answers(
    connector: Connector,
) -> None:
    client, install, _ = connector
    socket = install(
        [registry_entry("climate", "climate.hall", platform="demo", translation_key=None)],
        [{"id": "device1", "name": "Hall"}],
        [entity_state("climate.hall", "heat")],
        translations=TRANSLATIONS,
        silent=["frontend/get_translations"],
    )

    catalog = client.discover()

    assert [entity.supported for entity in catalog.entities] == [True]
    # The language read answered and the translations read was sent, then abandoned.
    assert [
        r["type"] for r in socket.sent if r["type"] in ("get_config", "frontend/get_translations")
    ] == [
        "get_config",
        "frontend/get_translations",
    ]
    assert socket.waits
    assert sum(wait for wait in socket.waits if wait is not None) <= 2
    # Without translations the label is the raw state.
    [reading] = client.read_states(["climate.hall"]).values()
    assert reading is not None
    assert reading.model_dump()["label"] == "heat"
