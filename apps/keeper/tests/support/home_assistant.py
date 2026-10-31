"""In-memory stand-ins for the Home Assistant websocket and REST wire."""

import json
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any, Self

import httpx
import pytest
from websockets.client import ClientProtocol
from websockets.frames import OP_TEXT
from websockets.frames import Frame as WebSocketFrame

from home_keeper.home_assistant import HomeAssistantConnector

# Raw Home Assistant frames are untyped JSON; tests build and mutate them freely.
type Frame = dict[str, Any]
type Handler = Callable[[httpx.Request], httpx.Response]


# Registry reads: the scripted answer for each, whatever order the adapter asks in.
REGISTRY_READS = ("config/entity_registry/list", "config/device_registry/list")


class FakeClock:
    """A clock the fake socket advances by the time each answer takes, so a slow Home Assistant
    costs no real time. Patch it in for the adapter's `time` module."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSocket:
    """A scripted Home Assistant websocket that records every frame the adapter sends.

    It answers each read by its command type, so request order does not matter. `failing`
    names command types that get an error result; `silent` names those answered only after
    the receive times out. Like Home Assistant, it refuses a request
    whose ID does not exceed every earlier ID on the connection.
    """

    def __init__(
        self,
        entities: list[Frame],
        devices: Iterable[Frame] = (),
        history: dict[str, list[Frame]] | None = None,
        *,
        statistics: dict[str, list[Frame]] | None = None,
        language: str = "en",
        translations: dict[str, str] | None = None,
        failing: Iterable[str] = (),
        silent: Iterable[str] = (),
        delays: dict[str, float] | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        # Seconds each answer to a command type takes on `clock`: a receive that waits less
        # times out (the answer arrives late); one that waits at least that long gets it.
        self.delays = delays or {}
        self.clock = clock
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
        # Long-term statistics rows by statistic ID, as `recorder/statistics_during_period`
        # returns them: `start` and `end` in epoch milliseconds, then `mean`, `min`, `max`.
        self.statistics: dict[str, list[Frame]] = statistics or {}
        self.language = language
        self.translations: dict[str, str] = translations or {}
        self.failing = set(failing)
        # Command types whose answer does not come within the wait, but arrives late.
        self.silent = set(silent)
        # The wait, in seconds, of each receive that timed out.
        self.waits: list[float | None] = []
        self._late: list[Frame] = []
        # The scripted registry answers; a test may edit them in place before discovery.
        self._registries = dict(zip(REGISTRY_READS, list(self.messages)[2:], strict=True))
        self._last_id = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def recv(self, timeout: float | None = None) -> str:
        if self._late:
            # A late answer to an earlier request comes before the next request's answer.
            return json.dumps(self._late.pop(0))
        assert self.messages, f"No scripted answer for {self.sent[-1:]}"
        message = self.messages.popleft()
        if "slow" in message:
            assert self.clock is not None, "A delay needs a clock"
            if timeout is not None and message["slow"] > timeout:
                self.clock.now += timeout
                self._late.append(message["reply"])
                raise TimeoutError
            self.clock.now += message["slow"]
            return json.dumps(message["reply"])
        if "late" in message:
            self.waits.append(timeout)
            self._late.append(message["late"])
            raise TimeoutError
        return json.dumps(message)

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
        reply = self._answer(identity, frame)
        if reply is None:
            return
        if frame.get("type") in self.delays:
            self.messages.appendleft({"slow": self.delays[frame["type"]], "reply": reply})
        elif frame.get("type") in self.silent:
            # Home Assistant never answers in time; the answer may still arrive late.
            self.messages.appendleft({"late": reply})
        else:
            self.messages.appendleft(reply)

    def _answer(self, identity: int, frame: Frame) -> Frame | None:
        command = frame.get("type")
        if command in self.failing:
            error = {"code": "unknown_error", "message": "Scripted failure."}
            return {"id": identity, "type": "result", "success": False, "error": error}
        result: Any
        if command in self._registries:
            scripted = self._registries[command]
            for position, message in enumerate(self.messages):
                if message is scripted:
                    del self.messages[position]
                    break
            return scripted | {"id": identity}
        if command == "history/history_during_period":
            result = {
                entity_id: self.history[entity_id]
                for entity_id in frame.get("entity_ids", [])
                if entity_id in self.history
            }
        elif command == "recorder/statistics_during_period":
            result = {
                statistic_id: self.statistics[statistic_id]
                for statistic_id in frame.get("statistic_ids", [])
                if statistic_id in self.statistics
            }
        elif command == "get_config":
            result = {"language": self.language}
        elif command == "frontend/get_translations":
            result = {"resources": self.translations}
        else:
            return None
        return {"id": identity, "type": "result", "success": True, "result": result}

    def close(self) -> None:
        pass


class Utf8Socket(FakeSocket):
    """A FakeSocket that sends UTF-8 JSON text frames, as Home Assistant does, without
    escaping non-ASCII text. `answer_bytes` records the UTF-8 size of each result frame."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.answer_bytes: list[int] = []

    def recv(self, timeout: float | None = None) -> str:
        text = json.dumps(json.loads(super().recv(timeout)), ensure_ascii=False)
        if json.loads(text).get("type") == "result":
            self.answer_bytes.append(len(text.encode("utf-8")))
        return text


class ProtocolSocket(FakeSocket):
    """A FakeSocket whose frames pass through the installed websockets library's real frame
    parser and its `max_size`, with no network. Set `protocol` to the client protocol first;
    a message over the limit raises the library's own close exception, as a real socket does."""

    protocol: ClientProtocol

    def recv(self, timeout: float | None = None) -> str:
        raw = super().recv(timeout)
        self.protocol.receive_data(WebSocketFrame(OP_TEXT, raw.encode()).serialize(mask=False))
        events = self.protocol.events_received()
        if self.protocol.parser_exc is not None:
            self.protocol.receive_eof()
            assert self.protocol.close_exc is not None
            raise self.protocol.close_exc
        [event] = events
        assert isinstance(event, WebSocketFrame)
        return bytes(event.data).decode()


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
    """Answers `GET /api/states` from `states`, and `GET /api/` with `api_status`.
    `requests` lists every request as (method, path). Any other path is a test failure."""

    def __init__(self, states: Iterable[Frame] = ()) -> None:
        self.states = {state["entity_id"]: state for state in states}
        self.requests: list[tuple[str, str]] = []
        self.api_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.url.path == "/api/":
            if self.api_status != 200:
                return httpx.Response(self.api_status, json={"message": "Unauthorized"})
            return httpx.Response(200, json={"message": "API running."})
        if request.url.path == "/api/states":
            return httpx.Response(200, json=list(self.states.values()))
        raise AssertionError(f"Unexpected request to {request.url.path}")


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


def realistic_entity(number: int, entity_id: str | None = None, **fields: Any) -> Frame:
    """One `config/entity_registry/list` row with the fields Home Assistant really sends, so
    thousands of them weigh what a large home's registry weighs (about 500 bytes each).
    Row `number` is a light on its own device unless `entity_id` says otherwise."""
    row: Frame = {
        "area_id": None,
        "categories": {},
        "config_entry_id": "01J5Z3Q8K2V7N4M9R6T1W0XY2B",
        "config_subentry_id": None,
        "device_id": f"{number:032x}",
        "disabled_by": None,
        "entity_category": None,
        "entity_id": entity_id or f"light.lamp_{number}",
        "has_entity_name": True,
        "hidden_by": None,
        "icon": None,
        "id": f"{number + 2**64:032x}",
        "labels": [],
        "name": None,
        "options": {"conversation": {"should_expose": False}},
        "original_name": f"Lamp {number}",
        "platform": "hue",
        "translation_key": "hue_light",
        "unique_id": f"00:17:88:01:0b:aa:{number:04x}-0b",
    }
    return row | fields


def realistic_device(number: int, **fields: Any) -> Frame:
    """One `config/device_registry/list` row with the fields Home Assistant really sends."""
    return {
        "area_id": None,
        "configuration_url": "https://192.168.1.20/",
        "config_entries": ["01J5Z3Q8K2V7N4M9R6T1W0XY2B"],
        "config_entries_subentries": {"01J5Z3Q8K2V7N4M9R6T1W0XY2B": [None]},
        "connections": [["mac", f"00:17:88:01:{number:08x}"]],
        "created_at": 1724500000.123456,
        "disabled_by": None,
        "entry_type": None,
        "hw_version": "1.2",
        "id": f"{number:032x}",
        "identifiers": [["hue", f"00178801{number:08x}"]],
        "labels": [],
        "manufacturer": "Signify Netherlands B.V.",
        "model": "Hue color lamp",
        "model_id": "LCT015",
        "modified_at": 1727000000.654321,
        "name_by_user": None,
        "name": f"Lamp {number}",
        "primary_config_entry": "01J5Z3Q8K2V7N4M9R6T1W0XY2B",
        "serial_number": None,
        "sw_version": "1.122.2",
        "via_device_id": None,
    } | fields


def light_state(entity: str = "light.bulb", **attributes: Any) -> Frame:
    """One entity state, as `GET /api/states` reports a light."""
    return {
        "entity_id": entity,
        "state": "on",
        "attributes": {"friendly_name": entity, **attributes},
        "last_changed": "2026-10-13T10:00:00Z",
        "last_updated": "2026-10-13T10:00:00Z",
        "context": {"id": "context1", "parent_id": None, "user_id": None},
    }


def entity_state(entity: str, state: str, **attributes: Any) -> Frame:
    """One entity state, as `GET /api/states` reports any domain."""
    return light_state(entity, **attributes) | {"state": state}


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


def statistics_row(start: datetime, mean: Any, low: Any, high: Any, *, hours: float = 1) -> Frame:
    """One row of `recorder/statistics_during_period`: the bucket from `start` for `hours`,
    with its times in epoch milliseconds. Pass `None` or leave a key out to test a bad row."""
    return {
        "start": start.timestamp() * 1000,
        "end": (start + timedelta(hours=hours)).timestamp() * 1000,
        "mean": mean,
        "min": low,
        "max": high,
    }


def history_row(
    state: str, changed: float, updated: float | None = None, **attributes: Any
) -> Frame:
    """One compressed row of `history/history_during_period` as Home Assistant 2026.10 sends
    it: Unix-second times, `lu` always, and `lc` left out when it equals `lu`."""
    row: Frame = {"s": state, "a": attributes, "lu": changed if updated is None else updated}
    if updated is not None and updated != changed:
        row["lc"] = changed
    return row
