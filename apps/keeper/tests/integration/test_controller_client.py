"""The API's controller client over real local HTTP: what it sends and how it reads replies."""

import json
import socket
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, ClassVar

import pytest
from pydantic import TypeAdapter

from home_keeper.control import ControllerUnreachable, HttpController
from home_keeper.models import LightCommand

SERVICE_TOKEN = "synthetic-service-token"
COMMAND: TypeAdapter[LightCommand] = TypeAdapter(LightCommand)


class Controller(BaseHTTPRequestHandler):
    """Records each request and answers with the class's scripted status and body."""

    requests: ClassVar[list[dict[str, Any]]] = []
    status: ClassVar[int] = 200
    body: ClassVar[bytes] = b'{"result": "ok"}'
    delay: ClassVar[float] = 0

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        Controller.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "json": json.loads(self.rfile.read(length)),
            }
        )
        time.sleep(Controller.delay)
        self.send_response(Controller.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(Controller.body)))
        self.end_headers()
        self.wfile.write(Controller.body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def controller(
    status: int = 200, body: bytes = b'{"result": "ok"}', delay: float = 0
) -> Generator[str]:
    Controller.requests = []
    Controller.status = status
    Controller.body = body
    Controller.delay = delay
    server = ThreadingHTTPServer(("127.0.0.1", 0), Controller)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


@pytest.mark.parametrize(
    "command", [{"action": "on", "brightness_pct": 30}, {"action": "on"}, {"action": "off"}]
)
def test_command_is_posted_with_the_service_credential(command: dict[str, object]) -> None:
    with controller() as url:
        result = HttpController(url, SERVICE_TOKEN).send(
            "light.desk", COMMAND.validate_python(command)
        )
    assert result == "ok"
    [request] = Controller.requests
    assert request["path"] == "/internal/command"
    assert request["authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert set(request["json"]) == {"source_entity_id", "command"}
    assert request["json"]["source_entity_id"] == "light.desk"
    assert without_none(request["json"]["command"]) == command


def test_refused_reply_is_refused() -> None:
    with controller(body=b'{"result": "refused"}') as url:
        result = HttpController(url, SERVICE_TOKEN).send(
            "light.desk", COMMAND.validate_python({"action": "off"})
        )
    assert result == "refused"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, b'{"detail": "Home Assistant unreachable"}'),
        (502, b"{}"),
        (401, b'{"detail": "Service authentication required"}'),
        (422, b'{"detail": "Invalid request"}'),
        (200, b'{"result": "maybe"}'),
        (200, b"not json"),
    ],
)
def test_failed_or_unreadable_reply_is_unreachable(status: int, body: bytes) -> None:
    with controller(status, body) as url, pytest.raises(ControllerUnreachable):
        HttpController(url, SERVICE_TOKEN).send(
            "light.desk", COMMAND.validate_python({"action": "on"})
        )


def test_controller_that_is_not_running_is_unreachable() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(ControllerUnreachable):
        HttpController(f"http://127.0.0.1:{port}", SERVICE_TOKEN).send(
            "light.desk", COMMAND.validate_python({"action": "on"})
        )


def test_controller_answer_inside_its_own_deadline_is_never_unreachable() -> None:
    # The controller gives up on Home Assistant after 4 s; the API waits up to 6 s for it.
    with controller(delay=5.5) as url:
        result = HttpController(url, SERVICE_TOKEN).send(
            "light.desk", COMMAND.validate_python({"action": "on"})
        )
    assert result == "ok"
