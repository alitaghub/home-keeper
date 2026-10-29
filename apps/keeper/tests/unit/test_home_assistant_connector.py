"""Home Assistant adapter against an in-memory websocket and a mocked REST transport."""

import json
import logging
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import httpx
import pytest
from support.home_assistant import (
    FakeSocket,
    Frame,
    StateServer,
    hue_room,
    install_socket,
    light_state,
    registry_entry,
    use_transport,
)

from home_keeper.home_assistant import (
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
NOT_SUPPORTED = "Only lights, plugs and switches are supported"
SETTING = "Settings and diagnostic switches are not recorded"
NO_DEVICE = "Switches without a device are not recorded"


@pytest.fixture
def connector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Connector]:
    """An adapter whose registries come from `install` and whose states from the server."""
    token = tmp_path / "token"
    token.write_text("secret-sentinel")
    server = StateServer()

    def install(
        entities: list[Frame], devices: Iterable[Frame] = (), states: Iterable[Frame] = ()
    ) -> FakeSocket:
        socket = FakeSocket(entities, devices)
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


def test_discovery_sends_only_the_two_registry_reads(connector: Connector) -> None:
    client, install, _ = connector
    socket = install([registry_entry()], states=[light_state()])
    client.discover()
    assert socket.sent[0]["type"] == "auth"
    assert socket.sent[1:] == [
        {"id": 1, "type": "config/entity_registry/list"},
        {"id": 2, "type": "config/device_registry/list"},
    ]


def test_discovery_reads_the_state_of_each_new_candidate_once(connector: Connector) -> None:
    client, install, server = connector
    huge = {"untrusted": "x" * MAX_PAYLOAD}
    entities = [
        registry_entry("bulb", "light.bulb"),
        registry_entry("plug", "switch.plug", platform="zha", translation_key=None),
        # None of these can be recorded whatever their state says, so none is read.
        registry_entry("sensor", "sensor.power"),
        registry_entry("helper", "light.helper", platform="group"),
        registry_entry("disabled", "light.disabled", disabled_by="user"),
        registry_entry("setting", "switch.child_lock", entity_category="config"),
        registry_entry("loose", "switch.template", device_id=None),
    ]
    install(
        entities,
        [{"id": "device1", "name": "Bulb"}],
        [
            light_state("light.bulb"),
            light_state("switch.plug"),
            *(
                light_state(entity, **huge)
                for entity in (
                    "sensor.power",
                    "light.helper",
                    "light.disabled",
                    "switch.child_lock",
                    "switch.template",
                )
            ),
        ],
    )
    first = client.discover()
    assert sorted(server.reads) == ["light.bulb", "switch.plug"]
    assert {entity.id for entity in first.entities if entity.supported} == {"ha:bulb", "ha:plug"}

    # A steady home costs no state reads.
    install(entities, [{"id": "device1", "name": "Bulb"}], server.states.values())
    assert client.discover() == first
    assert sorted(server.reads) == ["light.bulb", "switch.plug"]

    # Only the new candidate is read. An entity that left the registry is read again on return.
    server.reads.clear()
    install(
        [*entities[1:], registry_entry("desk", "light.desk")],
        [{"id": "device1", "name": "Bulb"}],
        [*server.states.values(), hue_room("light.desk")],
    )
    second = client.discover()
    assert server.reads == ["light.desk"]
    assert {entity.id: entity.reason for entity in second.entities}["ha:desk"] == GROUP
    install(entities, [{"id": "device1", "name": "Bulb"}], server.states.values())
    client.discover()
    assert server.reads == ["light.desk", "light.bulb"]


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
        "ha:sensor": (False, NOT_SUPPORTED, None),
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
        "ha:sensor": (False, NOT_SUPPORTED, None),
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
    ("response", "code"),
    [
        (httpx.Response(500, json={}), "discovery"),
        # None: Home Assistant cannot be reached.
        (None, "discovery"),
        (httpx.Response(200, content=b"{not json"), "invalid_data"),
        (httpx.Response(200, json=["light.bulb"]), "invalid_data"),
        (
            httpx.Response(
                200, json={"entity_id": "light.bulb", "state": "on", "attributes": ["x"]}
            ),
            "invalid_data",
        ),
        (httpx.Response(200, content=b"x" * (MAX_PAYLOAD + 1)), "invalid_data"),
    ],
    ids=[
        "server_error",
        "unreachable",
        "not_json",
        "not_an_object",
        "attributes_not_an_object",
        "oversized",
    ],
)
def test_a_failed_candidate_state_read_fails_discovery_and_is_retried_next_pass(
    connector: Connector, response: httpx.Response | None, code: str
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if response is None:
            raise httpx.ConnectError("refused", request=request)
        return response

    client, install, server = connector
    client.discover()
    install([registry_entry(), registry_entry("desk", "light.desk")])
    use_transport(client, reply)
    with pytest.raises(ConnectorError) as failure:
        client.discover()
    assert failure.value.code == code
    assert "secret-sentinel" not in str(failure.value)
    # The failed answer was not kept: the next pass reads the candidate again.
    server.reads.clear()
    use_transport(client, server)
    install(
        [registry_entry(), registry_entry("desk", "light.desk")], states=[hue_room("light.desk")]
    )
    catalog = client.discover()
    assert server.reads == ["light.desk"]
    assert {entity.id: entity.reason for entity in catalog.entities}["ha:desk"] == GROUP


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
    for entity_id in ("light.bulb/../../api/services", "sensor.power", "light.bulb?x=1"):
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
    assert len(socket.sent) == 2
    assert json.loads(json.dumps(socket.sent[1]))["type"] == "config/entity_registry/list"
