"""The controller's command endpoint: who may call it and which commands it accepts."""

from typing import Literal

import pytest
from fastapi.testclient import TestClient
from pydantic_core import to_jsonable_python

from home_keeper.control import ControllerUnreachable
from home_keeper_controller.server import backend, create_app
from home_keeper_light_command import ForwardedCommand

SERVICE_TOKEN = "synthetic-service-token"
SERVICE = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
VALID = {
    "device_kind": "light",
    "source_entity_id": "light.desk",
    "command": {"action": "on", "brightness_pct": 30},
}


type Reply = Literal["ok", "refused", "unreachable"]


class RecordingLights:
    """A device backend that records what reaches it."""

    def __init__(self, reply: Reply = "ok") -> None:
        self.reply: Reply = reply
        self.sent: list[object] = []

    def send(self, forwarded: ForwardedCommand) -> Literal["ok", "refused"]:
        self.sent.append(to_jsonable_python(forwarded, exclude_none=True))
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


def light(source: object, command: object, **extra: object) -> dict[str, object]:
    return {"device_kind": "light", "source_entity_id": source, "command": command, **extra}


def switch(source: object, command: object, **extra: object) -> dict[str, object]:
    return {"device_kind": "switch", "source_entity_id": source, "command": command, **extra}


ON = {"action": "on"}
OFF = {"action": "off"}


@pytest.mark.parametrize(
    "body",
    [
        # Each kind takes only its own Home Assistant domain.
        light("switch.plug", ON),
        switch("light.desk", ON),
        switch("light.desk", OFF),
        light("sensor.door", OFF),
        switch("sensor.door", OFF),
        light("lightx.desk", ON),
        light("xlight.desk", ON),
        switch("switchx.plug", ON),
        switch("xswitch.plug", ON),
        light("light_desk", ON),
        light("light.", ON),
        switch("switch.", ON),
        light("light.*", ON),
        switch("switch.*", ON),
        light("light.desk,light.reading", ON),
        light("light.desk;switch.plug", ON),
        switch("switch.plug;light.desk", ON),
        light(["light.desk"], ON),
        # A plug has no brightness and no other attributes.
        switch("switch.plug", {"action": "on", "brightness_pct": 30}),
        switch("switch.plug", {"action": "off", "brightness_pct": 30}),
        switch("switch.plug", {"action": "toggle"}),
        light("light.desk", {"action": "toggle"}),
        light("light.desk", {"action": "on", "brightness_pct": 0}),
        light("light.desk", {"action": "on", "brightness_pct": 101}),
        light("light.desk", {"action": "off", "brightness_pct": 50}),
        light("light.desk", {"action": "on", "rgb_color": [1, 2, 3]}),
        light("light.desk", ON, domain="switch"),
        switch("switch.plug", ON, domain="light"),
        light("light.desk", ON, service="toggle"),
        switch("switch.plug", ON, service="toggle"),
        light("light.desk", ON, service_data={}),
        # No other device kind, and no body without one.
        {"device_kind": "sensor", "source_entity_id": "sensor.door", "command": ON},
        {"device_kind": "cover", "source_entity_id": "cover.garage", "command": ON},
        {"source_entity_id": "light.desk", "command": ON},
        {"source_entity_id": "switch.plug", "command": ON},
        {"device_kind": "light", "source_entity_id": "light.desk"},
        {"device_kind": "switch", "command": ON},
        {"domain": "light", "service": "turn_on", "target": {"entity_id": "light.desk"}},
        {"domain": "switch", "service": "turn_on", "target": {"entity_id": "switch.plug"}},
    ],
)
def test_anything_but_a_fixed_light_or_switch_command_is_refused_before_home_assistant(
    body: dict[str, object],
) -> None:
    lights = RecordingLights()
    with TestClient(create_app(SERVICE_TOKEN, lights)) as client:
        response = client.post("/internal/command", json=body, headers=SERVICE)
    assert response.status_code == 422, response.text
    assert lights.sent == []


@pytest.mark.parametrize(
    "body",
    [
        light("light.hue_desk", {"action": "on", "brightness_pct": 1}),
        light("light.hue_desk", {"action": "on", "brightness_pct": 100}),
        light("light.hue_desk", ON),
        light("light.hue_desk", OFF),
        switch("switch.smart_plug", ON),
        switch("switch.smart_plug", OFF),
    ],
)
def test_fixed_light_or_switch_command_reaches_the_backend_unchanged(
    body: dict[str, object],
) -> None:
    lights = RecordingLights()
    with TestClient(create_app(SERVICE_TOKEN, lights)) as client:
        response = client.post("/internal/command", json=body, headers=SERVICE)
    assert (response.status_code, response.json()) == (200, {"result": "ok"})
    assert lights.sent == [body]


def test_backend_refusal_and_failure_are_reported_distinctly() -> None:
    refusing = RecordingLights("refused")
    with TestClient(create_app(SERVICE_TOKEN, refusing)) as client:
        refused = client.post("/internal/command", json=VALID, headers=SERVICE)
    assert (refused.status_code, refused.json()) == (200, {"result": "refused"})

    failing = RecordingLights("unreachable")
    with TestClient(create_app(SERVICE_TOKEN, failing)) as client:
        failed = client.post("/internal/command", json=VALID, headers=SERVICE)
    assert failed.status_code >= 500, failed.text
    assert failing.sent == [VALID]


def test_unknown_controller_backend_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="HOME_KEEPER_CONTROL_BACKEND"):
        backend("passthrough", SERVICE_TOKEN)
