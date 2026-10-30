"""Home Assistant adapter over a real local websocket and REST server."""

import json
import shutil
import ssl
import subprocess
from pathlib import Path
from threading import Thread

import pytest
from support.home_assistant import Frame, registry_entry
from websockets.datastructures import Headers
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

from home_keeper.home_assistant import ConnectorError, HomeAssistantConnector


def test_actual_websocket_discovery_and_rest_state_read(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("actual-network-token")
    transmitted: list[object] = []

    def process_request(_connection: ServerConnection, request: Request) -> Response | None:
        if request.path.startswith("/api/states"):
            transmitted.append(("GET", request.path))
            assert request.headers["Authorization"] == "Bearer actual-network-token"
            state: dict[str, object] = {"entity_id": "light.bulb", "state": "off", "attributes": {}}
            body = json.dumps(state if request.path == "/api/states/light.bulb" else [state])
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
            state = connector.read_states(["light.bulb"])["light.bulb"]
            assert state is not None
            assert state.state == "off"
        finally:
            connector.close()
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()
    # Discovery reads the new light's state once for group markers; then the state read.
    assert transmitted == [
        "auth",
        "config/entity_registry/list",
        "config/device_registry/list",
        ("GET", "/api/states/light.bulb"),
        ("GET", "/api/states"),
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
