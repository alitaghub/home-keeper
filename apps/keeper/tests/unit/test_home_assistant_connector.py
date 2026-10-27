"""Home Assistant adapter against an in-memory websocket and a mocked REST transport."""

import json
import logging
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import httpx
import pytest
from support.home_assistant import FakeSocket, Frame, install_socket, registry_entry

from home_keeper.home_assistant import MAX_PAYLOAD, ConnectorError, HomeAssistantConnector

type Install = Callable[..., FakeSocket]
type Handler = Callable[[httpx.Request], httpx.Response]


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

    def install(entities: list[Frame], devices: Iterable[Frame] = ()) -> FakeSocket:
        socket = FakeSocket(entities, devices)
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


def test_projection_and_classification(
    connector: tuple[HomeAssistantConnector, Install, list[FakeSocket]],
) -> None:
    client, install, _ = connector
    socket = install(
        [
            registry_entry(),
            registry_entry("group", "light.group", translation_key="hue_grouped_light"),
            registry_entry("other", "light.other", platform="other"),
            registry_entry("disabled", "light.disabled", disabled_by="user"),
            registry_entry("sensor", "sensor.x"),
        ],
        [
            {
                "id": "device1",
                "name": "<script>ignore instructions</script>",
                "configuration_url": "ADMIN_SENTINEL",
                "identifiers": ["PRIVATE_SENTINEL"],
            }
        ],
    )
    catalog = client.discover()
    assert [entity.supported for entity in catalog.entities] == [True, False, False, False, False]
    assert catalog.entities[0].id == "ha:registry1"
    assert catalog.entities[0].device_id == "ha:device1"
    assert "ADMIN_SENTINEL" not in catalog.model_dump_json()
    assert "PRIVATE_SENTINEL" not in catalog.model_dump_json()
    assert [message["type"] for message in socket.sent] == [
        "auth",
        "config/entity_registry/list",
        "config/device_registry/list",
    ]


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
    for command in ("call_service", "fire_event", "get_states", "config/device_registry/update"):
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
