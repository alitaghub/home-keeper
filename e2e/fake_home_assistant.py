"""Synthetic Home Assistant wire fixture for hardware-free deployment checks."""

import json
import time
from datetime import UTC, datetime
from typing import Any

from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

TOKEN = "synthetic-compose-token"
REGISTRY: dict[str, list[dict[str, object]]] = {
    "config/entity_registry/list": [
        {
            "id": "fixture",
            "entity_id": "light.fixture",
            "platform": "hue",
            "translation_key": "hue_light",
            "device_id": "fixture-device",
            "name": "Synthetic live bulb",
        },
        {
            "id": "unsupported",
            "entity_id": "light.other",
            "platform": "hue",
            "translation_key": "hue_grouped_light",
            "name": "Unsupported fixture",
        },
    ],
    "config/device_registry/list": [
        {
            "id": "fixture-device",
            "name": "Fixture device",
            "configuration_url": "ADMIN_SENTINEL",
        }
    ],
}
# light.fixture as `GET /api/states` reports it now.
light: dict[str, Any] = {"entity_id": "light.fixture"}
# light.fixture's recorder rows, oldest first, in the compressed form of
# `history/history_during_period`: Unix-second times, `lu` always, `lc` left out when it
# equals `lu`, as Home Assistant 2026.10 sends it.
history: list[dict[str, Any]] = []


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def change(at: float, state: str, **attributes: object) -> None:
    """light.fixture changes at `at`: its state now, and one more recorder row."""
    previous = history[-1] if history else None
    changed = (
        at if previous is None or previous["s"] != state else previous.get("lc", previous["lu"])
    )
    # The reader must drop attributes Home Keeper does not keep.
    reported = {"friendly_name": "Synthetic live bulb", "untrusted": "ADMIN_SENTINEL", **attributes}
    row: dict[str, Any] = {"s": state, "a": reported, "lu": at}
    if changed != at:
        row["lc"] = changed
    history.append(row)
    light.update(state=state, attributes=reported, last_changed=iso(changed), last_updated=iso(at))


def updated(row: dict[str, Any]) -> float:
    return row["lu"]


def history_during_period(command: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Home Assistant's answer: the state at the start, then each row inside the window."""
    if "light.fixture" not in command.get("entity_ids", []):
        return {}
    start = datetime.fromisoformat(command["start_time"]).timestamp()
    end = datetime.fromisoformat(command["end_time"]).timestamp()
    before = [row for row in history if updated(row) <= start][-1:]
    rows = before + [row for row in history if start < updated(row) < end]
    return {"light.fixture": rows} if rows else {}


# The light came on, then its brightness changed: History has one change from the start.
STARTED = time.time()
change(STARTED - 120, "on", brightness=120, color_mode="brightness")
change(STARTED - 60, "on", brightness=180, color_mode="brightness")


def http_response(_connection: ServerConnection, request: Request) -> Response | None:
    if request.path == "/api/":
        # Home Assistant's reachability check, which the reader's health route uses.
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return Response(401, "Unauthorized", Headers(), b"")
        body = json.dumps({"message": "API running."}).encode()
        return Response(200, "OK", Headers({"Content-Type": "application/json"}), body)
    if request.path.startswith("/api/states"):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return Response(401, "Unauthorized", Headers(), b"")
        # A room light: discovery reads its group markers and leaves it unsupported.
        room = {
            "entity_id": "light.other",
            "state": "off",
            "attributes": {"is_hue_group": True, "entity_id": ["light.fixture"]},
        }
        data: object
        if request.path == "/api/states":
            data = [light, room]
        elif request.path == "/api/states/light.fixture":
            data = light
        elif request.path == "/api/states/light.other":
            data = room
        else:
            return Response(404, "Not Found", Headers(), b"")
        return Response(
            200, "OK", Headers({"Content-Type": "application/json"}), json.dumps(data).encode()
        )
    return None


def websocket(socket: ServerConnection) -> None:
    socket.send(json.dumps({"type": "auth_required"}))
    auth = json.loads(socket.recv(timeout=5))
    if auth != {"type": "auth", "access_token": TOKEN}:
        socket.send(json.dumps({"type": "auth_invalid"}))
        return
    socket.send(json.dumps({"type": "auth_ok"}))
    try:
        while True:
            command = json.loads(socket.recv(timeout=60))
            kind = command.get("type")
            if kind == "history/history_during_period":
                success, result = True, history_during_period(command)
            else:
                success, result = kind in REGISTRY, REGISTRY.get(kind)
            socket.send(
                json.dumps(
                    {"id": command["id"], "type": "result", "success": success, "result": result}
                )
            )
    except (ConnectionClosed, TimeoutError):
        return


if __name__ == "__main__":
    # Listens on all interfaces inside its isolated Compose network only.
    with serve(websocket, "0.0.0.0", 8123, process_request=http_response) as server:  # noqa: S104
        server.serve_forever()
