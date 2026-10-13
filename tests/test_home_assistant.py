import json
from collections import deque

import httpx
import pytest

from home_observer.home_assistant import ConnectorError, HomeAssistantConnector


class Socket:
    def __init__(self, entities, devices=()):
        self.messages = deque(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": True, "result": entities},
                {"id": 2, "type": "result", "success": True, "result": list(devices)},
            ]
        )
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def recv(self, timeout):
        return json.dumps(self.messages.popleft())

    def send(self, value):
        self.sent.append(json.loads(value))


def row(identity="registry1", entity_id="light.bulb", **kwargs):
    return {
        "id": identity,
        "entity_id": entity_id,
        "platform": "hue",
        "translation_key": "hue_light",
        "device_id": "device1",
        **kwargs,
    }


@pytest.fixture
def connector(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("secret-sentinel")
    sockets = []

    def install(entities, devices=()):
        socket = Socket(entities, devices)
        sockets.append(socket)
        monkeypatch.setattr("home_observer.home_assistant.connect", lambda *a, **kw: socket)
        return socket

    install([row()])
    client = HomeAssistantConnector("http://ha.example:8123", token)
    yield client, install, sockets
    client.close()


def test_projection_and_classification(connector):
    client, install, _ = connector
    socket = install(
        [
            row(),
            row("group", "light.group", translation_key="hue_grouped_light"),
            row("other", "light.other", platform="other"),
            row("disabled", "light.disabled", disabled_by="user"),
            row("sensor", "sensor.x"),
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


def test_selected_reads_and_rename_identity(connector):
    client, install, _ = connector
    client.discover()
    client.approve({"ha:registry1": 7})
    calls = []

    def handle(request):
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

    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handle), base_url=client.origin)
    with pytest.raises(ConnectorError):
        client.read_selected("ha:unapproved", 7)
    with pytest.raises(ConnectorError):
        client.read_selected("ha:registry1", 8)
    assert not calls
    observation = client.read_selected("ha:registry1", 7)
    assert observation.entity_id == "ha:registry1"
    assert observation.brightness == 120
    assert "PRIVATE_SENTINEL" not in observation.model_dump_json()
    install([row(entity_id="light.renamed"), row("replacement")])
    client.discover()
    client.approve({"ha:registry1": 7})
    renamed = client.read_selected("ha:registry1", 7)
    assert renamed.source_entity_id == "light.renamed"
    assert calls[-1].url.path == "/api/states/light.renamed"
    with pytest.raises(ConnectorError):
        client.read_selected("ha:replacement", 7)
    assert all(call.method == "GET" for call in calls)


def test_failed_discovery_revokes_read_permission_and_redacts(connector):
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
def test_invalid_origin_safe(origin, tmp_path):
    with pytest.raises(ConnectorError) as error:
        HomeAssistantConnector(origin, tmp_path / "missing")
    assert "secret" not in str(error.value)


def test_actual_websocket_and_selected_rest(tmp_path):
    from threading import Thread

    from websockets.datastructures import Headers
    from websockets.http11 import Response
    from websockets.sync.server import serve

    token = tmp_path / "token"
    token.write_text("actual-network-token")
    transmitted = []

    def process_request(connection, request):
        if request.path.startswith("/api/states/"):
            transmitted.append(("GET", request.path))
            assert request.headers["Authorization"] == "Bearer actual-network-token"
            body = json.dumps({"entity_id": "light.bulb", "state": "off", "attributes": {}})
            return Response(200, "OK", Headers({"Content-Type": "application/json"}), body.encode())
        return None

    def handler(socket):
        socket.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(socket.recv())
        transmitted.append(auth["type"])
        assert auth["access_token"] == "actual-network-token"
        socket.send(json.dumps({"type": "auth_ok"}))
        for result in ([row()], [{"id": "device1", "name": "Synthetic bulb"}]):
            message = json.loads(socket.recv())
            transmitted.append(message["type"])
            socket.send(
                json.dumps(
                    {"id": message["id"], "type": "result", "success": True, "result": result}
                )
            )

    with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        connector = HomeAssistantConnector(
            f"http://127.0.0.1:{server.socket.getsockname()[1]}", token
        )
        try:
            assert connector.discover().entities[0].supported
            connector.approve({"ha:registry1": 3})
            assert connector.read_selected("ha:registry1", 3).state == "off"
        finally:
            connector.close()
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()
    assert transmitted == [
        "auth",
        "config/entity_registry/list",
        "config/device_registry/list",
        ("GET", "/api/states/light.bulb"),
    ]


def test_actual_untrusted_tls_rejected(tmp_path):
    import shutil
    import ssl
    import subprocess
    from threading import Thread

    from websockets.sync.server import serve

    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl needed to generate temporary self-signed test certificate")
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    token = tmp_path / "token"
    token.write_text("tls-secret-sentinel")
    with serve(lambda socket: None, "127.0.0.1", 0, ssl=context) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        connector = HomeAssistantConnector(
            f"https://127.0.0.1:{server.socket.getsockname()[1]}", token
        )
        try:
            with pytest.raises(ConnectorError) as error:
                connector.discover()
            assert error.value.code == "tls"
            assert "tls-secret-sentinel" not in str(error.value)
        finally:
            connector.close()
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()


def test_missing_failed_reads_and_recovery(connector):
    client, _, _ = connector
    client.discover()
    client.approve({"ha:registry1": 1})
    status = [404]
    client._http.close()
    client._http = httpx.Client(
        base_url=client.origin,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status[0], json={"entity_id": "light.bulb", "state": "on", "attributes": {}}
            )
        ),
    )
    with pytest.raises(ConnectorError) as error:
        client.read_selected("ha:registry1", 1)
    assert error.value.code == "missing"
    status[0] = 200
    assert client.read_selected("ha:registry1", 1).state == "on"


def test_names_redact_credentials_and_write_helper_rejected(connector):
    client, install, _ = connector
    client._redactions = ("secret-sentinel", "internal-sentinel")
    socket = install([row(name="secret-sentinel internal-sentinel")])
    assert client.discover().entities[0].name == "[redacted] [redacted]"
    count = len(socket.sent)
    for command in ("call_service", "fire_event", "get_states", "config/device_registry/update"):
        with pytest.raises(ConnectorError):
            client._registry(socket, 3, command)
    assert len(socket.sent) == count


def test_live_cycle_api_discovery_selection_failure_recovery(tmp_path):
    from threading import Thread

    from fastapi.testclient import TestClient
    from websockets.datastructures import Headers
    from websockets.http11 import Response
    from websockets.sync.server import serve

    from home_observer.api import create_app
    from home_observer.live import reconcile

    token = tmp_path / "ha-token"
    token.write_text("ha-token-sentinel")
    entities = [
        row(),
        row("group", "light.group", translation_key="hue_grouped_light"),
        row("plug", "switch.plug", platform="other"),
    ]
    fail = [False]
    state_reads = []

    def process_request(connection, request):
        if request.path.startswith("/api/states/"):
            state_reads.append(request.path)
            state_id = request.path.rsplit("/", 1)[-1]
            payload = {
                "entity_id": state_id,
                "state": "on",
                "attributes": {"brightness": 180, "secret_unused": "UNSELECTED_SENTINEL"},
            }
            return Response(
                200,
                "OK",
                Headers({"Content-Type": "application/json"}),
                json.dumps(payload).encode(),
            )
        return None

    def handler(socket):
        socket.send(json.dumps({"type": "auth_required"}))
        socket.recv()
        socket.send(json.dumps({"type": "auth_invalid" if fail[0] else "auth_ok"}))
        if fail[0]:
            return
        for result in (
            entities,
            [
                {
                    "id": "device1",
                    "name": "Synthetic Hue bulb",
                    "configuration_url": "ADMIN_SENTINEL",
                }
            ],
        ):
            command = json.loads(socket.recv())
            socket.send(
                json.dumps(
                    {"id": command["id"], "type": "result", "success": True, "result": result}
                )
            )

    db = tmp_path / "live.sqlite"
    app = create_app(
        db_path=db, service_token="internal-token-sentinel", source_id="home_assistant"
    )
    with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        connector = HomeAssistantConnector(
            f"http://127.0.0.1:{server.socket.getsockname()[1]}",
            token,
            redaction_values=("internal-token-sentinel",),
        )
        try:
            with TestClient(
                app, headers={"Authorization": "Bearer internal-token-sentinel"}
            ) as api:
                reconcile(api, connector)
                assert not state_reads
                catalog = api.get("/api/catalog").json()
                assert len(catalog["entities"]) == 3
                assert sum(entity["supported"] for entity in catalog["entities"]) == 1
                assert api.put(
                    "/api/selection/ha:registry1",
                    json={"selected": True},
                    headers={"X-Home-Observer": "1"},
                ).is_success
                reconcile(api, connector)
                baseline = api.get("/api/monitored").json()["entities"][0]["baseline"]
                assert baseline["state"] == "on"
                assert state_reads == ["/api/states/light.bulb"]
                entities.append(row("new", "light.new"))
                reconcile(api, connector)
                assert len(api.get("/api/catalog").json()["entities"]) == 4
                assert "ha:new" not in api.get("/api/selection").json()["entity_ids"]
                fail[0] = True
                reconcile(api, connector)
                assert api.get("/api/catalog").json()["freshness"] == "stale"
                assert api.put(
                    "/api/selection/ha:new",
                    json={"selected": True},
                    headers={"X-Home-Observer": "1"},
                ).is_success
                assert (
                    api.put(
                        "/api/selection/ha:unknown",
                        json={"selected": True},
                        headers={"X-Home-Observer": "1"},
                    ).status_code
                    == 404
                )
                count = len(state_reads)
                reconcile(api, connector)
                assert len(state_reads) == count
                fail[0] = False
                reconcile(api, connector)
                assert set(state_reads) == {"/api/states/light.bulb", "/api/states/light.new"}
                assert all(
                    entity["baseline"] for entity in api.get("/api/monitored").json()["entities"]
                )
                wire = api.get("/api/catalog").text + api.get("/api/monitored").text
                for sentinel in (
                    "ha-token-sentinel",
                    "internal-token-sentinel",
                    "ADMIN_SENTINEL",
                    "UNSELECTED_SENTINEL",
                ):
                    assert sentinel not in wire
                    assert sentinel.encode() not in db.read_bytes()
        finally:
            connector.close()
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()


def test_payload_limit_and_contradictory_state_rejected(connector):
    client, _, _ = connector
    client.discover()
    client.approve({"ha:registry1": 1})
    for response in (
        httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
        httpx.Response(200, json={"entity_id": "light.other", "state": "on"}),
        httpx.Response(200, json={"entity_id": "light.bulb", "state": "secret-sentinel"}),
    ):
        client._http.close()
        client._http = httpx.Client(
            base_url=client.origin,
            transport=httpx.MockTransport(lambda request, response=response: response),
        )
        with pytest.raises(ConnectorError) as error:
            client.read_selected("ha:registry1", 1)
        assert error.value.code == "invalid_data"
        assert "secret-sentinel" not in str(error.value)


def test_slow_stream_total_time_is_bounded(connector, monkeypatch):
    client, _, _ = connector
    client.discover()
    client.approve({"ha:registry1": 1})

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"entity_id": "light.bulb",'
            yield b'"state": "on"}'

    clock = iter([0, 1, 6])
    monkeypatch.setattr("home_observer.home_assistant.time.monotonic", lambda: next(clock))
    client._http.close()
    client._http = httpx.Client(
        base_url=client.origin,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())),
    )
    with pytest.raises(ConnectorError) as error:
        client.read_selected("ha:registry1", 1)
    assert error.value.code == "timeout"


def test_startup_configuration_failure_posts_safe_status(tmp_path, monkeypatch, caplog):
    from home_observer import live

    service_token = tmp_path / "service-token"
    service_token.write_text("internal-startup-sentinel")
    ha_token = tmp_path / "ha-token"
    ha_token.write_text("ha-startup-sentinel")
    monkeypatch.setenv("OBSERVER_TOKEN_FILE", str(service_token))
    monkeypatch.setenv("OBSERVER_HA_TOKEN_FILE", str(ha_token))
    monkeypatch.setenv("OBSERVER_HA_URL", "http://user:ha-startup-sentinel@host")
    calls = []
    api_client = httpx.Client(
        base_url="http://api:8000",
        transport=httpx.MockTransport(
            lambda request: calls.append(json.loads(request.content)) or httpx.Response(200)
        ),
    )
    monkeypatch.setattr(live.httpx, "Client", lambda **kwargs: api_client)
    with pytest.raises(SystemExit) as error:
        live.main()
    assert error.value.code == 1
    assert calls[0]["error"] == "configuration"
    assert "internal-startup-sentinel" not in caplog.text
    assert "ha-startup-sentinel" not in caplog.text
