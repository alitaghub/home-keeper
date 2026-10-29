"""Home Assistant adapter and subscription over a real local websocket and REST server."""

import contextlib
import json
import shutil
import ssl
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from fastapi.testclient import TestClient
from support.api import recorded_current, recorded_events
from support.home_assistant import Frame, hue_room, registry_entry, state_changed
from support.recorded_reader import RecordedReader
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

from home_keeper.api import create_app
from home_keeper.home_assistant import ConnectorError, HomeAssistantConnector
from home_keeper.live import CollectionSession
from home_keeper.models import LightState


def test_actual_websocket_and_selected_rest(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("actual-network-token")
    transmitted: list[object] = []

    def process_request(_connection: ServerConnection, request: Request) -> Response | None:
        if request.path.startswith("/api/states/"):
            transmitted.append(("GET", request.path))
            assert request.headers["Authorization"] == "Bearer actual-network-token"
            body = json.dumps({"entity_id": "light.bulb", "state": "off", "attributes": {}})
            return Response(200, "OK", Headers({"Content-Type": "application/json"}), body.encode())
        return None

    def handler(socket: ServerConnection) -> None:
        socket.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(socket.recv())
        transmitted.append(auth["type"])
        assert auth["access_token"] == "actual-network-token"
        socket.send(json.dumps({"type": "auth_ok"}))
        results: tuple[list[Frame], ...] = (
            [registry_entry(platform="zha", translation_key=None)],
            [{"id": "device1", "name": "Synthetic bulb"}],
        )
        for result in results:
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
    # Discovery reads the new light's state once for group markers; then the selected read.
    assert transmitted == [
        "auth",
        "config/entity_registry/list",
        "config/device_registry/list",
        ("GET", "/api/states/light.bulb"),
        ("GET", "/api/states/light.bulb"),
    ]


def test_actual_untrusted_tls_rejected(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl needed to generate temporary self-signed test certificate")
    certificate, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(  # noqa: S603 -- fixed argv with a resolved openssl path
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

    def handler(_socket: ServerConnection) -> None:
        pass

    with serve(handler, "127.0.0.1", 0, ssl=context) as server:
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


def test_live_cycle_api_discovery_selection_failure_recovery(tmp_path: Path) -> None:
    token = tmp_path / "ha-token"
    token.write_text("ha-token-sentinel")
    entities = [
        registry_entry(),
        registry_entry("group", "light.group", translation_key="hue_grouped_light"),
        registry_entry("plug", "switch.plug", platform="other"),
    ]
    unused = {"secret_unused": "UNSELECTED_SENTINEL"}
    # The room carries Hue's group marker; only discovery's group answer may be kept from it.
    groups = {"light.group": hue_room("light.group")["attributes"]}
    fail = [False]
    state_reads: list[str] = []

    def process_request(_connection: ServerConnection, request: Request) -> Response | None:
        if request.path.startswith("/api/states/"):
            state_reads.append(request.path)
            state_id = request.path.rsplit("/", 1)[-1]
            payload = {
                "entity_id": state_id,
                "state": "on",
                "attributes": {"brightness": 180, **unused, **groups.get(state_id, {})},
            }
            return Response(
                200,
                "OK",
                Headers({"Content-Type": "application/json"}),
                json.dumps(payload).encode(),
            )
        return None

    def handler(socket: ServerConnection) -> None:
        socket.send(json.dumps({"type": "auth_required"}))
        socket.recv()
        socket.send(json.dumps({"type": "auth_invalid" if fail[0] else "auth_ok"}))
        if fail[0]:
            return
        command = json.loads(socket.recv(timeout=5))
        if command["type"] == "subscribe_events":
            socket.send(json.dumps({"id": 1, "type": "result", "success": True}))
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                socket.recv(timeout=10)
            return
        results: tuple[list[Frame], ...] = (
            entities,
            [
                {
                    "id": "device1",
                    "name": "Synthetic Hue bulb",
                    "configuration_url": "ADMIN_SENTINEL",
                }
            ],
        )
        for index, result in enumerate(results):
            socket.send(
                json.dumps(
                    {"id": command["id"], "type": "result", "success": True, "result": result}
                )
            )
            if index < len(results) - 1:
                command = json.loads(socket.recv(timeout=5))

    db = tmp_path / "live.sqlite"
    app = create_app(
        db_path=db,
        service_token="internal-token-sentinel",
        source_id="home_assistant",
        reader=RecordedReader(db),
    )
    with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        connector = HomeAssistantConnector(
            f"http://127.0.0.1:{server.socket.getsockname()[1]}",
            token,
            redaction_values=("internal-token-sentinel",),
        )
        session: CollectionSession | None = None
        try:
            with TestClient(
                app, headers={"Authorization": "Bearer internal-token-sentinel"}
            ) as api:
                session = CollectionSession(api, connector)
                session.reconcile()
                # Discovery reads each candidate's state once, for group markers only.
                discovered = {
                    "/api/states/light.bulb",
                    "/api/states/light.group",
                    "/api/states/switch.plug",
                }
                assert sorted(state_reads) == sorted(discovered)
                catalog = api.get("/api/catalog").json()
                assert len(catalog["entities"]) == 3
                # The bulb and the plug on device1; the room is a group.
                assert sum(entity["supported"] for entity in catalog["entities"]) == 2
                assert api.put(
                    "/api/selection/ha:registry1",
                    json={"selected": True},
                    headers={"X-Home-Keeper": "1"},
                ).is_success
                session.reconcile()
                assert recorded_current(db, "ha:registry1")["state"] == "on"
                assert state_reads[3:] == ["/api/states/light.bulb"]
                entities.append(registry_entry("new", "light.new"))
                session.reconcile()
                # The new light is read once for group markers; the selected bulb is refreshed.
                assert state_reads[4:].count("/api/states/light.new") == 1
                assert len(api.get("/api/catalog").json()["entities"]) == 4
                assert "ha:new" not in api.get("/api/selection").json()["entity_ids"]
                fail[0] = True
                session.reconcile()
                assert api.put(
                    "/api/selection/ha:new",
                    json={"selected": True},
                    headers={"X-Home-Keeper": "1"},
                ).is_success
                assert (
                    api.put(
                        "/api/selection/ha:unknown",
                        json={"selected": True},
                        headers={"X-Home-Keeper": "1"},
                    ).status_code
                    == 404
                )
                count = len(state_reads)
                session.reconcile()
                assert len(state_reads) == count
                fail[0] = False
                session.reconcile()
                assert set(state_reads) == {*discovered, "/api/states/light.new"}
                for entity_id in api.get("/api/selection").json()["entity_ids"]:
                    assert recorded_current(db, entity_id)["state"]
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
            if session is not None:
                session.close()
            connector.close()
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()


def test_actual_selected_subscription_and_revoked_revision(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("wire-stream-secret")
    commands: list[Any] = []

    def process_request(_connection: ServerConnection, request: Request) -> Response | None:
        if request.path.startswith("/api/states/"):
            # The bulb has no state yet, so it is not a group.
            return Response(404, "Not Found", Headers(), b"")
        return None

    def handler(socket: ServerConnection) -> None:
        socket.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(socket.recv(timeout=5))
        assert auth["access_token"] == "wire-stream-secret"
        socket.send(json.dumps({"type": "auth_ok"}))
        request = json.loads(socket.recv(timeout=5))
        commands.append(request["type"])
        if request["type"] == "subscribe_events":
            assert request["event_type"] == "state_changed"
            socket.send(json.dumps({"id": 1, "type": "result", "success": True, "result": None}))
            socket.send(json.dumps(state_changed("light.unselected", state="PRIVATE_SENTINEL")))
            socket.send(json.dumps(state_changed()))
            with contextlib.suppress(ConnectionClosed):
                socket.recv(timeout=5)
        else:
            assert request["type"] == "config/entity_registry/list"
            socket.send(
                json.dumps(
                    {"id": 1, "type": "result", "success": True, "result": [registry_entry()]}
                )
            )
            request = json.loads(socket.recv(timeout=5))
            commands.append(request["type"])
            assert request["type"] == "config/device_registry/list"
            socket.send(json.dumps({"id": 2, "type": "result", "success": True, "result": []}))

    with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        connector = HomeAssistantConnector(
            f"http://127.0.0.1:{server.socket.getsockname()[1]}", token
        )
        stream = None
        try:
            connector.discover()
            connector.approve({"ha:registry1": 2})
            stream = connector.subscribe_selected()
            assert stream is not None
            assert stream.receive(1) is None
            received = stream.receive(1)
            assert received is not None
            assert received.entity_id == "ha:registry1"
            assert isinstance(received.new, LightState)
            assert received.new.brightness == 180
            assert "PRIVATE_SENTINEL" not in received.model_dump_json()
            assert "wire-stream-secret" not in received.model_dump_json()
            connector.approve({})
            assert stream.receive(1) is None
        finally:
            if stream:
                stream.close()
            connector.close()
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()
    assert commands == [
        "config/entity_registry/list",
        "config/device_registry/list",
        "subscribe_events",
    ]


def test_selected_switch_records_a_baseline_and_a_change_without_light_attributes(
    tmp_path: Path,
) -> None:
    token = tmp_path / "ha-token"
    token.write_text("ha-token-sentinel")
    plug = {"platform": "zha", "translation_key": None}
    entities = [registry_entry("plug", "switch.plug", **plug)]
    # Set once the baseline is stored, so the change is newer than the snapshot.
    recorded = Event()

    def process_request(_connection: ServerConnection, request: Request) -> Response | None:
        if request.path.startswith("/api/states/"):
            # A plug reporting light attributes: they must never be stored.
            payload = {
                "entity_id": "switch.plug",
                "state": "on",
                "last_changed": "2026-10-13T09:00:00Z",
                "attributes": {"brightness": 200, "color_mode": "brightness"},
            }
            return Response(
                200,
                "OK",
                Headers({"Content-Type": "application/json"}),
                json.dumps(payload).encode(),
            )
        return None

    def handler(socket: ServerConnection) -> None:
        socket.send(json.dumps({"type": "auth_required"}))
        socket.recv(timeout=5)
        socket.send(json.dumps({"type": "auth_ok"}))
        command = json.loads(socket.recv(timeout=5))
        if command["type"] == "subscribe_events":
            socket.send(json.dumps({"id": 1, "type": "result", "success": True}))
            assert recorded.wait(5)
            # state_changed carries brightness on both states; the plug turns off.
            now = datetime.now(UTC).isoformat()
            change = state_changed("switch.plug", state="off", last_updated=now)
            change["event"]["time_fired"] = now
            socket.send(json.dumps(change))
            with contextlib.suppress(ConnectionClosed, TimeoutError):
                socket.recv(timeout=10)
            return
        results: tuple[list[Frame], ...] = (
            entities,
            [{"id": "device1", "name": "Smart plug"}],
        )
        for index, result in enumerate(results):
            socket.send(
                json.dumps(
                    {"id": command["id"], "type": "result", "success": True, "result": result}
                )
            )
            if index < len(results) - 1:
                command = json.loads(socket.recv(timeout=5))

    db = tmp_path / "live.sqlite"
    app = create_app(
        db_path=db,
        service_token="internal-token-sentinel",
        source_id="home_assistant",
        reader=RecordedReader(tmp_path / "live.sqlite"),
    )
    with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        connector = HomeAssistantConnector(
            f"http://127.0.0.1:{server.socket.getsockname()[1]}", token
        )
        session: CollectionSession | None = None
        try:
            with TestClient(
                app, headers={"Authorization": "Bearer internal-token-sentinel"}
            ) as api:
                session = CollectionSession(api, connector)
                session.reconcile()
                [entry] = api.get("/api/catalog").json()["entities"]
                assert (entry["supported"], entry.get("device_kind")) == (True, "switch")
                assert api.put(
                    "/api/selection/ha:plug",
                    json={"selected": True},
                    headers={"X-Home-Keeper": "1"},
                ).is_success
                assert session.reconcile()
                recorded.set()
                deadline = time.monotonic() + 5
                while not recorded_events(db) and time.monotonic() < deadline:
                    session.receive(0.05)

                [baseline] = api.get("/api/baselines?entity_id=ha:plug").json()["baselines"]
                assert baseline["device_kind"] == "switch"
                assert baseline["state"] == "on"
                [event] = recorded_events(db)
                assert event["device_kind"] == "switch"
                assert (event["old"]["state"], event["new"]["state"]) == ("on", "off")
                [monitored] = api.get("/api/monitored").json()["entities"]
                assert monitored["device_kind"] == "switch"
                assert monitored["state"]["device_kind"] == "switch"
                assert monitored["state"]["state"] == "off"
                light_fields = {"brightness", "color_mode"}
                for reading in (baseline, event["old"], event["new"], monitored["state"]):
                    assert not light_fields & reading.keys(), reading
        finally:
            if session is not None:
                session.close()
            connector.close()
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()
