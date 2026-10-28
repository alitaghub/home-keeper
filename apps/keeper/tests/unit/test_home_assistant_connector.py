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
    hue_room,
    install_socket,
    light_state,
    registry_entry,
)

from home_keeper.home_assistant import MAX_PAYLOAD, ConnectorError, HomeAssistantConnector
from home_keeper.models import Observation

type Install = Callable[..., FakeSocket]
type Handler = Callable[[httpx.Request], httpx.Response]

GROUP = "Groups and rooms are not recorded"
DISABLED = "Disabled in Home Assistant"
NOT_SUPPORTED = "Only lights, plugs and switches are supported"
SETTING = "Settings and diagnostic switches are not recorded"
NO_DEVICE = "Switches without a device are not recorded"


def use_transport(client: HomeAssistantConnector, handler: Handler) -> None:
    """Swap the adapter's REST client for one served by `handler`."""
    client._http.close()  # pyright: ignore[reportPrivateUsage] -- tests replace the transport
    client._http = httpx.Client(  # pyright: ignore[reportPrivateUsage] -- tests replace the transport
        base_url=client.origin, transport=httpx.MockTransport(handler)
    )


@pytest.fixture
def connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[HomeAssistantConnector, Install, list[FakeSocket]]]:
    token = tmp_path / "token"
    token.write_text("secret-sentinel")
    sockets: list[FakeSocket] = []

    def install(
        entities: list[Frame],
        devices: Iterable[Frame] = (),
        states: Iterable[Frame] = (),
        states_reply: Frame | None = None,
    ) -> FakeSocket:
        socket = FakeSocket(entities, devices, states, states_reply)
        sockets.append(socket)
        install_socket(monkeypatch, socket)
        return socket

    install([registry_entry()])
    client = HomeAssistantConnector("http://ha.example:8123", token)
    yield client, install, sockets
    client.close()


@pytest.fixture
def adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HomeAssistantConnector]:
    secret = tmp_path / "token"
    secret.write_text("review-ha-secret")
    install_socket(monkeypatch, FakeSocket([registry_entry()]))
    client = HomeAssistantConnector("http://ha.example:8123", secret)
    client.discover()
    client.approve({"ha:registry1": 1})
    yield client
    client.close()


def test_projection_keeps_device_names_and_drops_private_registry_fields(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
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


def test_discovery_reads_registries_then_states_in_order(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
) -> None:
    client, install, _ = connector
    socket = install([registry_entry()], states=[light_state()])
    client.discover()
    assert socket.sent[0]["type"] == "auth"
    assert socket.sent[1:] == [
        {"id": 1, "type": "config/entity_registry/list"},
        {"id": 2, "type": "config/device_registry/list"},
        {"id": 3, "type": "get_states"},
    ]


def test_any_enabled_single_light_is_supported_and_groups_rooms_and_others_are_not(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
) -> None:
    client, install, _ = connector
    no_translation = {"translation_key": None}
    install(
        [
            registry_entry("hue", "light.bulb"),
            registry_entry("zha", "light.zha_bulb", platform="zha", **no_translation),
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
    client.approve({"ha:zha": 1, "ha:stateless": 1})
    with pytest.raises(ConnectorError):
        client.approve({"ha:room": 1})


def test_a_plug_or_switch_on_a_device_is_supported_and_settings_groups_and_loose_ones_are_not(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
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
            # The group rule comes before the switch-only category rule.
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
        "ha:group_setting": (False, GROUP, "switch"),
        "ha:sensor": (False, NOT_SUPPORTED, None),
    }
    client.approve({"ha:plug": 1})
    for unsupported in ("ha:config", "ha:loose", "ha:members", "ha:sensor"):
        with pytest.raises(ConnectorError):
            client.approve({unsupported: 1})


def test_a_selected_switch_reads_its_state_without_light_attributes(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
) -> None:
    client, install, _ = connector
    install(
        [registry_entry("plug", "switch.plug", platform="zha", translation_key=None)],
        [{"id": "device1", "name": "Smart plug"}],
    )
    client.discover()
    client.approve({"ha:plug": 4})
    use_transport(
        client,
        lambda request: httpx.Response(
            200,
            json={
                "entity_id": "switch.plug",
                "state": "on",
                "last_changed": "2026-10-01T10:00:00Z",
                "attributes": {"brightness": 120, "color_mode": "brightness"},
            },
        ),
    )
    reading = client.read_selected("ha:plug", 4).model_dump(exclude_none=True)
    assert reading["device_kind"] == "switch"
    assert reading["state"] == "on"
    assert reading["availability"] == "available"
    assert "brightness" not in reading
    assert "color_mode" not in reading


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ({"id": 3, "type": "result", "success": False, "error": {"code": "x"}}, "discovery"),
        ({"id": 3, "type": "result", "success": True, "result": {"x": {}}}, "invalid_data"),
        ({"id": 3, "type": "result", "success": True, "result": ["light.bulb"]}, "invalid_data"),
        (
            {
                "id": 3,
                "type": "result",
                "success": True,
                "result": [{"entity_id": "light.bulb", "state": "on", "attributes": ["x"]}],
            },
            "invalid_data",
        ),
        ({"id": 9, "type": "result", "success": True, "result": []}, "invalid_data"),
    ],
    ids=["failed", "not_a_list", "row_not_an_object", "attributes_not_an_object", "wrong_id"],
)
def test_failed_or_malformed_states_fail_discovery_and_revoke_reads(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]], reply: Frame, code: str
) -> None:
    client, install, _ = connector
    client.discover()
    client.approve({"ha:registry1": 1})
    install([registry_entry()], states_reply=reply)
    with pytest.raises(ConnectorError) as failure:
        client.discover()
    assert failure.value.code == code
    with pytest.raises(ConnectorError):
        client.approve({"ha:registry1": 1})
    with pytest.raises(ConnectorError):
        client.read_selected("ha:registry1", 1)


def test_selected_reads_and_rename_identity(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
) -> None:
    client, install, _ = connector
    client.discover()
    client.approve({"ha:registry1": 7})
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "entity_id": request.url.path.split("/")[-1],
                "state": "on",
                "last_changed": "2026-10-01T10:00:00Z",
                "attributes": {"brightness": 120, "context": "PRIVATE_SENTINEL"},
            },
        )

    use_transport(client, handle)
    with pytest.raises(ConnectorError):
        client.read_selected("ha:unapproved", 7)
    with pytest.raises(ConnectorError):
        client.read_selected("ha:registry1", 8)
    assert not calls
    observation = client.read_selected("ha:registry1", 7)
    assert isinstance(observation, Observation)
    assert observation.entity_id == "ha:registry1"
    assert observation.brightness == 120
    assert "PRIVATE_SENTINEL" not in observation.model_dump_json()
    install([registry_entry(entity_id="light.renamed"), registry_entry("replacement")])
    client.discover()
    client.approve({"ha:registry1": 7})
    renamed = client.read_selected("ha:registry1", 7)
    assert renamed.source_entity_id == "light.renamed"
    assert calls[-1].url.path == "/api/states/light.renamed"
    with pytest.raises(ConnectorError):
        client.read_selected("ha:replacement", 7)
    assert all(call.method == "GET" for call in calls)


def test_failed_discovery_revokes_read_permission_and_redacts(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
) -> None:
    client, install, _ = connector
    client.discover()
    client.approve({"ha:registry1": 1})
    socket = install([])
    socket.messages[1] = {"type": "auth_invalid", "message": "secret-sentinel"}
    with pytest.raises(ConnectorError) as failure:
        client.discover()
    assert failure.value.code == "authentication"
    assert "secret-sentinel" not in str(failure.value)
    with pytest.raises(ConnectorError):
        client.read_selected("ha:registry1", 1)


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


def test_missing_failed_reads_and_recovery(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
) -> None:
    client, _, _ = connector
    client.discover()
    client.approve({"ha:registry1": 1})
    status = [404]
    use_transport(
        client,
        lambda _request: httpx.Response(
            status[0], json={"entity_id": "light.bulb", "state": "on", "attributes": {}}
        ),
    )
    with pytest.raises(ConnectorError) as error:
        client.read_selected("ha:registry1", 1)
    assert error.value.code == "missing"
    status[0] = 200
    assert client.read_selected("ha:registry1", 1).state == "on"


def test_names_redact_credentials_and_write_helper_rejected(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
) -> None:
    client, install, _ = connector
    client._redactions = ("secret-sentinel", "internal-sentinel")  # pyright: ignore[reportPrivateUsage] -- inject redactions
    socket = install([registry_entry(name="secret-sentinel internal-sentinel")])
    assert client.discover().entities[0].name == "[redacted] [redacted]"
    count = len(socket.sent)
    for command in ("call_service", "fire_event", "config/device_registry/update"):
        with pytest.raises(ConnectorError):
            client._registry(socket, 3, command)  # pyright: ignore[reportPrivateUsage] -- exercise the private write guard directly
    assert len(socket.sent) == count


def test_payload_limit_and_contradictory_state_rejected(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
) -> None:
    client, _, _ = connector
    client.discover()
    client.approve({"ha:registry1": 1})
    for response in (
        httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
        httpx.Response(200, json={"entity_id": "light.other", "state": "on"}),
        httpx.Response(200, json={"entity_id": "light.bulb", "state": "secret-sentinel"}),
    ):
        use_transport(client, lambda _request, response=response: response)
        with pytest.raises(ConnectorError) as error:
            client.read_selected("ha:registry1", 1)
        assert error.value.code == "invalid_data"
        assert "secret-sentinel" not in str(error.value)


def test_slow_stream_total_time_is_bounded(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = connector
    client.discover()
    client.approve({"ha:registry1": 1})

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b'{"entity_id": "light.bulb",'
            yield b'"state": "on"}'

    clock = iter([0, 1, 6])
    monkeypatch.setattr("home_keeper.home_assistant.time.monotonic", lambda: next(clock))
    use_transport(client, lambda _request: httpx.Response(200, stream=Stream()))
    with pytest.raises(ConnectorError) as error:
        client.read_selected("ha:registry1", 1)
    assert error.value.code == "timeout"


@pytest.mark.parametrize("kind", ["oversized", "wrong_identity", "unsupported_state", "redirect"])
def test_invalid_selected_response_is_safe(
    adapter: HomeAssistantConnector, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    payload: dict[str, object] = {"entity_id": "light.bulb", "state": "on", "attributes": {}}
    if kind == "oversized":
        payload["untrusted"] = "review-ha-secret" + "x" * MAX_PAYLOAD
    if kind == "wrong_identity":
        payload["entity_id"] = "light.unapproved"
    if kind == "unsupported_state":
        payload["state"] = "review-ha-secret"
    calls: list[httpx.URL] = []

    def response(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(
            302 if kind == "redirect" else 200,
            json=payload,
            headers={"Location": "http://admin.example/write"},
        )

    use_transport(adapter, response)
    with caplog.at_level(logging.DEBUG), pytest.raises(ConnectorError) as error:
        adapter.read_selected("ha:registry1", 1)
    assert len(calls) == 1
    assert calls[0].path == "/api/states/light.bulb"
    assert "review-ha-secret" not in str(error.value)
    assert "review-ha-secret" not in caplog.text


def test_timeout_and_discovery_revoke_do_not_leak(
    adapter: HomeAssistantConnector,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("review-ha-secret", request=request)

    use_transport(adapter, timeout)
    with caplog.at_level(logging.DEBUG), pytest.raises(ConnectorError) as error:
        adapter.read_selected("ha:registry1", 1)
    assert error.value.code == "timeout"
    assert "review-ha-secret" not in str(error.value)
    assert "review-ha-secret" not in caplog.text
    install_socket(monkeypatch, FakeSocket([registry_entry("replacement")]))
    adapter.discover()
    with pytest.raises(ConnectorError):
        adapter.approve({"ha:registry1": 1})
    with pytest.raises(ConnectorError):
        adapter.read_selected("ha:registry1", 1)


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
