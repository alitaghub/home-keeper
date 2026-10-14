"""In-memory stand-ins for the Home Assistant websocket wire."""

import json
from collections import deque
from collections.abc import Iterable
from typing import Any, Self

import pytest

# Raw Home Assistant frames are untyped JSON; tests build and mutate them freely.
type Frame = dict[str, Any]


class FakeSocket:
    """Replays a scripted registry conversation and records every frame the adapter sends."""

    def __init__(self, entities: list[Frame], devices: Iterable[Frame] = ()) -> None:
        self.messages: deque[Frame] = deque(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": True, "result": entities},
                {"id": 2, "type": "result", "success": True, "result": list(devices)},
            ]
        )
        self.sent: list[Frame] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def recv(self, timeout: float | None = None) -> str:
        return json.dumps(self.messages.popleft())

    def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    def close(self) -> None:
        pass


def install_socket(monkeypatch: pytest.MonkeyPatch, socket: FakeSocket) -> None:
    """Make the adapter's websocket connect return `socket`."""

    def connect(*_args: object, **_kwargs: object) -> FakeSocket:
        return socket

    monkeypatch.setattr("home_observer.home_assistant.connect", connect)


def registry_entry(
    identity: str = "registry1", entity_id: str = "light.bulb", **fields: Any
) -> Frame:
    return {
        "id": identity,
        "entity_id": entity_id,
        "platform": "hue",
        "translation_key": "hue_light",
        "device_id": "device1",
        **fields,
    }


def state_changed(entity: str = "light.bulb", **changes: Any) -> Frame:
    state: Frame = {
        "entity_id": entity,
        "state": "on",
        "attributes": {"brightness": 120},
        "last_updated": "2026-10-13T10:00:00Z",
    }
    return {
        "id": 1,
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": "2026-10-13T10:00:00Z",
            "data": {
                "entity_id": entity,
                "old_state": state,
                "new_state": state
                | {"attributes": {"brightness": 180, "untrusted": "PRIVATE_SENTINEL"}, **changes},
            },
        },
    }
