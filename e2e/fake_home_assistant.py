"""Synthetic Home Assistant wire fixture for hardware-free deployment checks."""

import json
from datetime import UTC, datetime

from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

TOKEN = "synthetic-compose-token"
# Each subscription toggles the fixture light's brightness, so consecutive events differ.
light = {"brightness": 120}


def http_response(_connection: ServerConnection, request: Request) -> Response | None:
    if request.path == "/api/":
        # Home Assistant's reachability check, which the reader's health route uses.
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return Response(401, "Unauthorized", Headers(), b"")
        body = json.dumps({"message": "API running."}).encode()
        return Response(200, "OK", Headers({"Content-Type": "application/json"}), body)
    if request.path.startswith("/api/states/"):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return Response(401, "Unauthorized", Headers(), b"")
        if request.path == "/api/states/light.fixture":
            data = {
                "entity_id": "light.fixture",
                "state": "off",
                "last_changed": "2026-01-01T00:00:00Z",
                "attributes": {"brightness": light["brightness"]},
            }
        elif request.path == "/api/states/light.other":
            # A room light: discovery reads its group markers and leaves it unsupported.
            data = {
                "entity_id": "light.other",
                "state": "off",
                "attributes": {"is_hue_group": True, "entity_id": ["light.fixture"]},
            }
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
    results: dict[str, list[dict[str, object]]] = {
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
    command = json.loads(socket.recv(timeout=5))
    if command["type"] == "subscribe_events":
        assert command["event_type"] == "state_changed"
        socket.send(
            json.dumps({"id": command["id"], "type": "result", "success": True, "result": None})
        )
        fired = datetime.now(UTC).isoformat()
        old = {
            "entity_id": "light.fixture",
            "state": "off",
            "attributes": {"brightness": light["brightness"]},
        }
        light["brightness"] = 180 if light["brightness"] == 120 else 120
        new = old | {"attributes": {"brightness": light["brightness"]}, "last_updated": fired}
        socket.send(
            json.dumps(
                {
                    "id": command["id"],
                    "type": "event",
                    "event": {
                        "event_type": "state_changed",
                        "time_fired": fired,
                        "data": {"entity_id": "light.fixture", "old_state": old, "new_state": new},
                    },
                }
            )
        )
        try:
            while True:
                socket.recv(timeout=60)
        except ConnectionClosed:
            return
    else:
        for index in range(len(results)):
            socket.send(
                json.dumps(
                    {
                        "id": command["id"],
                        "type": "result",
                        "success": command["type"] in results,
                        "result": results.get(command["type"]),
                    }
                )
            )
            if index < len(results) - 1:
                command = json.loads(socket.recv(timeout=5))


if __name__ == "__main__":
    # Listens on all interfaces inside its isolated Compose network only.
    with serve(websocket, "0.0.0.0", 8123, process_request=http_response) as server:  # noqa: S104
        server.serve_forever()
