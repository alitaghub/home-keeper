"""Synthetic connector. No device, vendor, network-administration or model access.

With the `control` profile, the demo controller forwards light and switch commands here,
and they switch the synthetic home's lights and plug.
"""

import json
import logging
import secrets
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Literal, TypedDict, overload

import httpx
from pydantic import ValidationError

from home_keeper.events import change_event
from home_keeper.models import (
    DeviceKind,
    LightCommand,
    LightState,
    SelectionSnapshot,
    SwitchState,
)
from home_keeper.settings import ServiceSettings
from home_keeper.tokens import load_token
from home_keeper_light_command import (
    BRIGHTNESS_MAX,
    ForwardedCommand,
    ForwardResult,
    LightOff,
    LightOn,
    SwitchCommand,
    SwitchOff,
    SwitchOn,
    brightness_value,
    forwarded_command,
)
from home_keeper_settings import SettingsError, load

# The replay phase whose events are delivered twice.
DUPLICATE_PHASE = 3
# A switched light keeps its commanded state for this many replay passes.
HELD_PASSES = 3
# The demo command endpoint, reachable on the internal `control` network only.
COMMAND_PORT = 8080
MAX_COMMAND_BYTES = 64 * 1024


class DemoEntity(TypedDict):
    id: str
    device_id: str | None
    device_name: str | None
    name: str
    device_kind: DeviceKind | None
    supported: bool
    reason: str | None


ENTITIES: list[DemoEntity] = [
    {
        "id": "light.hue_desk",
        "device_id": "hue-desk",
        "device_name": "Desk lamp",
        "name": "Desk light",
        "device_kind": "light",
        "supported": True,
        "reason": None,
    },
    {
        "id": "light.hue_reading",
        "device_id": "hue-reading",
        "device_name": "Reading lamp",
        "name": "Reading light <script>alert('untrusted')</script>",
        "device_kind": "light",
        "supported": True,
        "reason": None,
    },
    {
        "id": "light.hue_room",
        "device_id": "hue-room",
        "device_name": "Living room group",
        "name": "All living room lights",
        "device_kind": "light",
        "supported": False,
        "reason": "Groups and rooms are not recorded",
    },
    {
        "id": "switch.smart_plug",
        "device_id": "smart-plug",
        "device_name": "Smart plug",
        "name": "Plug",
        "device_kind": "switch",
        "supported": True,
        "reason": None,
    },
    {
        "id": "sensor.ungrouped",
        "device_id": None,
        "device_name": None,
        "name": "Ungrouped sensor",
        "device_kind": None,
        "supported": False,
        "reason": "Only lights, plugs and switches are supported",
    },
]
CATALOG = {"source_id": "demo", "entities": ENTITIES}


def coverage(
    client: httpx.Client, status: str, reason: str = "connection", revision: int | None = None
) -> None:
    client.post(
        "/internal/coverage",
        json={
            "source_id": "demo",
            "status": status,
            "reason": reason,
            "selection_revision": revision,
            "received_at": datetime.now(UTC).isoformat(),
        },
    ).raise_for_status()


class DemoReplay:
    """Repeatable synthetic transitions. Unselected history is never forwarded."""

    def __init__(self) -> None:
        self.phase = 0
        # Commands and replay passes change the same states from two threads.
        self.lock = Lock()
        # Lights a command switched, with the replay passes they still skip.
        self.held: dict[str, int] = {}
        # Each light's brightness from when it was last on, which an off state does not carry.
        self.last_brightness: dict[str, int] = {}
        since = {"last_changed": "2025-01-01T12:00:00Z", "last_updated": "2025-01-01T12:00:00Z"}
        self.states: dict[str, LightState | SwitchState] = {
            entity["id"]: SwitchState.model_validate(
                since | {"state": "on", "device_kind": "switch"}
            )
            if entity["device_kind"] == "switch"
            else LightState.model_validate(
                since
                | {"state": "on", "brightness": 180 if entity["id"] == "light.hue_desk" else 100}
            )
            for entity in ENTITIES
            if entity["supported"]
        }

    def advance(self, event_time: datetime) -> dict[str, LightState | SwitchState]:
        sequence: tuple[dict[str, object], ...] = (
            {"state": "off"},
            {"state": "on", "brightness": 100},
            {"state": "on", "brightness": 40},
            {"state": "on", "color_mode": "color_temp", "color_temp_kelvin": 2700},
            {"state": "on", "color_mode": "xy", "xy_color": (0.4, 0.3)},
            {"state": "unavailable"},
            {"state": "on", "brightness": 180, "color_mode": "brightness"},
        )
        value = sequence[self.phase % len(sequence)]
        self.phase += 1
        previous = self.states
        self.last_brightness |= {
            identity: old.brightness
            for identity, old in previous.items()
            if isinstance(old, LightState) and old.brightness is not None
        }
        self.states = {
            identity: old if identity in self.held else self._next(old, value, event_time)
            for identity, old in previous.items()
        }
        self.held = {identity: passes - 1 for identity, passes in self.held.items() if passes > 1}
        return previous

    @staticmethod
    def _next[State: LightState | SwitchState](
        old: State, value: dict[str, object], event_time: datetime
    ) -> State:
        times = {
            "last_changed": event_time if old.state != value["state"] else old.last_changed,
            "last_updated": event_time,
            "last_reported": event_time,
        }
        if isinstance(old, SwitchState):
            # A plug follows the lights' on, off and unavailable steps, without their attributes.
            return old.model_validate({"state": value["state"], "device_kind": "switch"} | times)
        return old.model_validate(value | times)

    @overload
    def switch(
        self, identity: str, command: LightCommand, event_time: datetime
    ) -> tuple[LightState, LightState] | None: ...
    @overload
    def switch(
        self, identity: str, command: SwitchCommand, event_time: datetime
    ) -> tuple[SwitchState, SwitchState] | None: ...
    def switch(
        self, identity: str, command: LightCommand | SwitchCommand, event_time: datetime
    ) -> tuple[LightState, LightState] | tuple[SwitchState, SwitchState] | None:
        """Apply a command and hold the device; returns (old, new), or None if it cannot switch."""
        old = self.states.get(identity)
        if old is None or old.state not in ("on", "off"):
            return None
        if isinstance(old, SwitchState):
            if not isinstance(command, SwitchOn | SwitchOff):
                return None
            switched = self._next(old, {"state": command.action}, event_time)
            self.states[identity] = switched
            self.held[identity] = HELD_PASSES
            return old, switched
        if not isinstance(command, LightOn | LightOff):
            return None
        if old.brightness is not None:
            self.last_brightness[identity] = old.brightness
        if isinstance(command, LightOff):
            value: dict[str, object] = {"state": "off"}
        else:
            # Like Home Assistant, turning on without a brightness keeps the last one.
            brightness = (
                brightness_value(command.brightness_pct)
                if command.brightness_pct is not None
                else self.last_brightness.get(identity, BRIGHTNESS_MAX)
            )
            value = {"state": "on", "brightness": brightness, "color_mode": "brightness"}
        new = self._next(old, value, event_time)
        self.states[identity] = new
        self.held[identity] = HELD_PASSES
        return old, new


def switch_device(
    client: httpx.Client, replay: DemoReplay, forwarded: ForwardedCommand
) -> Literal["ok", "refused"]:
    """Switch a synthetic light or plug and record the change at once, as Home Assistant
    would."""
    identity = forwarded.source_entity_id
    with replay.lock:
        fired = datetime.now(UTC)
        change = replay.switch(identity, forwarded.command, fired)
        if change is None:
            return "refused"
        response = client.get("/internal/selection")
        response.raise_for_status()
        selection = SelectionSnapshot.model_validate(response.json())
        if identity in selection.entity_ids:
            old, new = change
            event = change_event(
                source_id="demo",
                entity_id=identity,
                source_entity_id=identity,
                revision=selection.revision,
                event_time=fired,
                old=old,
                new=new,
            )
            response = client.post(
                "/internal/events", json=event.model_dump(mode="json", exclude_none=True)
            )
            if response.status_code != HTTPStatus.CONFLICT:
                response.raise_for_status()
    return "ok"


def command_server(
    client: httpx.Client, replay: DemoReplay, token: str, port: int = COMMAND_PORT
) -> ThreadingHTTPServer:
    """The demo controller's target. It needs the service credential, like the API."""
    expected = f"Bearer {token}".encode()

    class CommandHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/internal/command":
                self._reply(HTTPStatus.NOT_FOUND, {"detail": "Not found"})
                return
            supplied = self.headers.get("Authorization", "").encode("utf-8", "replace")
            if not secrets.compare_digest(supplied, expected):
                self._reply(HTTPStatus.UNAUTHORIZED, {"detail": "Service authentication required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= MAX_COMMAND_BYTES:
                    raise ValueError
                forwarded = forwarded_command.validate_json(self.rfile.read(length))
            except (ValueError, ValidationError):
                self._reply(HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": "Invalid request"})
                return
            try:
                result = switch_device(client, replay, forwarded)
            except (httpx.HTTPError, ValueError):
                logging.warning("Demo command could not be recorded")
                self._reply(HTTPStatus.BAD_GATEWAY, {"detail": "Command not recorded"})
                return
            self._reply(HTTPStatus.OK, ForwardResult(result=result).model_dump())

        def _reply(self, status: HTTPStatus, body: dict[str, str]) -> None:
            content = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            # Request lines are not logged; the reconciliation loop logs failures.
            pass

    return ThreadingHTTPServer(("0.0.0.0", port), CommandHandler)  # noqa: S104 -- reachable only on the internal control network


def reconcile(client: httpx.Client, replay: DemoReplay) -> None:
    client.post("/internal/catalog", json=CATALOG).raise_for_status()
    selection = client.get("/internal/selection")
    selection.raise_for_status()
    selected = selection.json()
    approved = [identity for identity in selected["entity_ids"] if identity in replay.states]
    for identity in approved:
        observation = replay.states[identity].model_dump(mode="json", exclude_none=True) | {
            "entity_id": identity,
            "source_entity_id": identity,
            "selection_revision": selected["revision"],
            "source_id": "demo",
            "received_at": datetime.now(UTC).isoformat(),
        }
        response = client.post("/internal/baselines", json=observation)
        if response.status_code != HTTPStatus.CONFLICT:
            response.raise_for_status()
    fired = datetime.now(UTC)
    previous = replay.advance(fired)
    for identity in approved:
        if previous[identity] == replay.states[identity]:
            # A held light did not change on this pass.
            continue
        event = change_event(
            source_id="demo",
            entity_id=identity,
            source_entity_id=identity,
            revision=selected["revision"],
            event_time=fired,
            old=previous[identity],
            new=replay.states[identity],
        )
        payload = event.model_dump(mode="json", exclude_none=True)
        # Replay one duplicate to demonstrate idempotent delivery without duplicate evidence.
        for _ in range(2 if replay.phase == DUPLICATE_PHASE else 1):
            response = client.post("/internal/events", json=payload)
            if response.status_code != HTTPStatus.CONFLICT:
                response.raise_for_status()
    client.post(
        "/internal/health",
        json={
            "source_id": "demo",
            "status": "healthy",
            "received_at": datetime.now(UTC).isoformat(),
        },
    ).raise_for_status()
    coverage(client, "recovered", revision=selected["revision"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = load(ServiceSettings)
    except SettingsError as error:
        logging.error("Demo collector configuration invalid: %s", error)
        raise SystemExit(1) from None
    while not settings.token_file.exists():
        time.sleep(1)
    token = load_token(settings.token_file)
    with httpx.Client(
        # The demo has one fixed API host, not a user-supplied HTTP passthrough.
        base_url=f"http://api:{settings.port}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
        trust_env=False,
    ) as client:
        replay = DemoReplay()
        server = command_server(client, replay, token)
        Thread(target=server.serve_forever, name="demo-commands", daemon=True).start()
        while True:
            try:
                with replay.lock:
                    reconcile(client, replay)
            except (httpx.HTTPError, ValueError):
                # Never log request headers, responses or exception strings.
                logging.warning("Demo collection failed; retrying on next reconciliation")
                try:
                    coverage(client, "interrupted", "ingestion")
                    client.post(
                        "/internal/health",
                        json={
                            "source_id": "demo",
                            "status": "failed",
                            "received_at": datetime.now(UTC).isoformat(),
                        },
                    )
                except httpx.HTTPError:
                    pass
            time.sleep(settings.reconciliation_seconds)


if __name__ == "__main__":
    main()
