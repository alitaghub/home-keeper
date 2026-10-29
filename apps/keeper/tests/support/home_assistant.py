"""In-memory stand-ins for the Home Assistant websocket and REST wire."""

import json
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any, Self

import httpx
import pytest

from home_keeper.home_assistant import HomeAssistantConnector

# Raw Home Assistant frames are untyped JSON; tests build and mutate them freely.
type Frame = dict[str, Any]
type Handler = Callable[[httpx.Request], httpx.Response]


class FakeSocket:
    """Replays the two registry answers and records every frame the adapter sends.

    It answers `history/history_during_period` from `history`: Home Assistant's compressed
    rows by entity ID, returned as they are for each requested entity it has. Like Home
    Assistant, it refuses a request whose ID does not exceed every earlier ID on the
    connection.
    """

    def __init__(
        self,
        entities: list[Frame],
        devices: Iterable[Frame] = (),
        history: dict[str, list[Frame]] | None = None,
    ) -> None:
        self.messages: deque[Frame] = deque(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": True, "result": entities},
                {"id": 2, "type": "result", "success": True, "result": list(devices)},
            ]
        )
        self.sent: list[Frame] = []
        self.history: dict[str, list[Frame]] = history or {}
        self._last_id = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def recv(self, timeout: float | None = None) -> str:
        assert self.messages, f"No scripted answer for {self.sent[-1:]}"
        return json.dumps(self.messages.popleft())

    def send(self, value: str) -> None:
        frame = json.loads(value)
        self.sent.append(frame)
        if frame.get("type") == "auth":
            # Tests reuse one socket for several connections; IDs restart with each one.
            self._last_id = 0
        identity = frame.get("id")
        if not isinstance(identity, int):
            return
        if identity <= self._last_id:
            error = {"code": "id_reuse", "message": "Identifier values have to increase."}
            self.messages.appendleft(
                {"id": identity, "type": "result", "success": False, "error": error}
            )
            return
        self._last_id = identity
        if frame.get("type") == "history/history_during_period":
            result = {
                entity_id: self.history[entity_id]
                for entity_id in frame.get("entity_ids", [])
                if entity_id in self.history
            }
            self.messages.appendleft(
                {"id": identity, "type": "result", "success": True, "result": result}
            )

    def close(self) -> None:
        pass


def install_socket(monkeypatch: pytest.MonkeyPatch, socket: FakeSocket) -> None:
    """Make the adapter's websocket connect return `socket`."""

    def connect(*_args: object, **_kwargs: object) -> FakeSocket:
        return socket

    monkeypatch.setattr("home_keeper.home_assistant.connect", connect)


def use_transport(client: HomeAssistantConnector, handler: Handler) -> None:
    """Swap the adapter's REST client for one served by `handler`."""
    client._http.close()  # pyright: ignore[reportPrivateUsage] -- tests replace the transport
    client._http = httpx.Client(  # pyright: ignore[reportPrivateUsage] -- tests replace the transport
        base_url=client.origin, transport=httpx.MockTransport(handler)
    )


class StateServer:
    """Answers `GET /api/states/{entity_id}` from `states`, 404 for any other entity, and
    Home Assistant's reachability check `GET /api/` with `api_status`.

    `reads` lists each requested entity id in order; `requests` lists every request as
    (method, path).
    """

    def __init__(self, states: Iterable[Frame] = ()) -> None:
        self.states = {state["entity_id"]: state for state in states}
        self.reads: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.api_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/api/":
            if self.api_status != 200:
                return httpx.Response(self.api_status, json={"message": "Unauthorized"})
            return httpx.Response(200, json={"message": "API running."})
        entity_id = request.url.path.removeprefix("/api/states/")
        self.reads.append(entity_id)
        if entity_id not in self.states:
            return httpx.Response(404, json={"message": "Entity not found."})
        return httpx.Response(200, json=self.states[entity_id])


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


def light_state(entity: str = "light.bulb", **attributes: Any) -> Frame:
    """One entity state, as `GET /api/states/{entity_id}` reports a light."""
    return {
        "entity_id": entity,
        "state": "on",
        "attributes": {"friendly_name": entity, **attributes},
        "last_changed": "2026-10-13T10:00:00Z",
        "last_updated": "2026-10-13T10:00:00Z",
        "context": {"id": "context1", "parent_id": None, "user_id": None},
    }


def hue_room(entity: str = "light.living_room") -> Frame:
    """The state Home Assistant's Hue integration reports for a room."""
    return light_state(
        entity,
        is_hue_group=True,
        hue_type="room",
        entity_id=["light.bulb", "light.desk"],
    )


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


def history_row(
    state: str, changed: float, updated: float | None = None, **attributes: Any
) -> Frame:
    """One compressed row of `history/history_during_period`: Unix-second times, and `lu`
    left out when it equals `lc`, as Home Assistant sends it."""
    row: Frame = {"s": state, "a": attributes, "lc": changed}
    if updated is not None and updated != changed:
        row["lu"] = updated
    return row
