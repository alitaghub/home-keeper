"""The controller's command endpoint: who may call it and which commands it accepts."""

from typing import Literal

import pytest
from fastapi.testclient import TestClient
from pydantic_core import to_jsonable_python

from home_keeper.control import ControllerUnreachable
from home_keeper_controller.server import create_app

SERVICE_TOKEN = "synthetic-service-token"
SERVICE = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
VALID = {"source_entity_id": "light.desk", "command": {"action": "on", "brightness_pct": 30}}


type Reply = Literal["ok", "refused", "unreachable"]


class RecordingLights:
    """A light backend that records what reaches it."""

    def __init__(self, reply: Reply = "ok") -> None:
        self.reply: Reply = reply
        self.sent: list[tuple[str, object]] = []

    def send(self, source_entity_id: str, command: object) -> Literal["ok", "refused"]:
        self.sent.append((source_entity_id, to_jsonable_python(command, exclude_none=True)))
        reply = self.reply
        if reply == "unreachable":
            raise ControllerUnreachable
        return reply


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-credential"},
        {"Authorization": SERVICE_TOKEN},
        {"Authorization": f"Basic {SERVICE_TOKEN}"},
        {"X-Home-Keeper": "1"},
    ],
)
def test_command_without_the_service_credential_is_unauthorized(
    headers: dict[str, str],
) -> None:
    lights = RecordingLights()
    with TestClient(create_app(SERVICE_TOKEN, lights)) as client:
        response = client.post("/internal/command", json=VALID, headers=headers)
    assert response.status_code == 401, response.text
    assert SERVICE_TOKEN not in response.text
    assert lights.sent == []


@pytest.mark.parametrize(
    "body",
    [
        {"source_entity_id": "switch.plug", "command": {"action": "on"}},
        {"source_entity_id": "sensor.door", "command": {"action": "off"}},
        {"source_entity_id": "lightx.desk", "command": {"action": "on"}},
        {"source_entity_id": "xlight.desk", "command": {"action": "on"}},
        {"source_entity_id": "light_desk", "command": {"action": "on"}},
        {"source_entity_id": "light.", "command": {"action": "on"}},
        {"source_entity_id": "light.*", "command": {"action": "on"}},
        {"source_entity_id": "light.desk,light.reading", "command": {"action": "on"}},
        {"source_entity_id": "light.desk;switch.plug", "command": {"action": "on"}},
        {"source_entity_id": ["light.desk"], "command": {"action": "on"}},
        {"source_entity_id": "light.desk", "command": {"action": "toggle"}},
        {"source_entity_id": "light.desk", "command": {"action": "on", "brightness_pct": 0}},
        {"source_entity_id": "light.desk", "command": {"action": "on", "brightness_pct": 101}},
        {"source_entity_id": "light.desk", "command": {"action": "off", "brightness_pct": 50}},
        {"source_entity_id": "light.desk", "command": {"action": "on", "rgb_color": [1, 2, 3]}},
        {"source_entity_id": "light.desk", "command": {"action": "on"}, "domain": "switch"},
        {"source_entity_id": "light.desk", "command": {"action": "on"}, "service": "toggle"},
        {"source_entity_id": "light.desk", "command": {"action": "on"}, "service_data": {}},
        {"source_entity_id": "light.desk"},
        {"command": {"action": "on"}},
        {"domain": "light", "service": "turn_on", "target": {"entity_id": "light.desk"}},
    ],
)
def test_anything_but_a_fixed_light_command_is_refused_before_home_assistant(
    body: dict[str, object],
) -> None:
    lights = RecordingLights()
    with TestClient(create_app(SERVICE_TOKEN, lights)) as client:
        response = client.post("/internal/command", json=body, headers=SERVICE)
    assert response.status_code == 422, response.text
    assert lights.sent == []


@pytest.mark.parametrize(
    "command",
    [
        {"action": "on", "brightness_pct": 1},
        {"action": "on", "brightness_pct": 100},
        {"action": "on"},
        {"action": "off"},
    ],
)
def test_fixed_light_command_reaches_the_backend_unchanged(command: dict[str, object]) -> None:
    lights = RecordingLights()
    with TestClient(create_app(SERVICE_TOKEN, lights)) as client:
        response = client.post(
            "/internal/command",
            json={"source_entity_id": "light.hue_desk", "command": command},
            headers=SERVICE,
        )
    assert (response.status_code, response.json()) == (200, {"result": "ok"})
    assert lights.sent == [("light.hue_desk", command)]


def test_backend_refusal_and_failure_are_reported_distinctly() -> None:
    refusing = RecordingLights("refused")
    with TestClient(create_app(SERVICE_TOKEN, refusing)) as client:
        refused = client.post("/internal/command", json=VALID, headers=SERVICE)
    assert (refused.status_code, refused.json()) == (200, {"result": "refused"})

    failing = RecordingLights("unreachable")
    with TestClient(create_app(SERVICE_TOKEN, failing)) as client:
        failed = client.post("/internal/command", json=VALID, headers=SERVICE)
    assert failed.status_code >= 500, failed.text
    assert failing.sent == [("light.desk", VALID["command"])]
