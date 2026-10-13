"""Synthetic Home Assistant wire fixture for hardware-free deployment checks."""

import json
from datetime import UTC, datetime

from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from websockets.sync.server import serve

TOKEN = "synthetic-compose-token"
BRIGHTNESS = 120


def http_response(connection, request):
    if request.path.startswith("/api/states/"):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return Response(401, "Unauthorized", Headers(), b"")
        if request.path != "/api/states/light.fixture":
            return Response(404, "Not Found", Headers(), b"")
        data = {
            "entity_id": "light.fixture",
            "state": "off",
            "last_changed": "2026-01-01T00:00:00Z",
            "attributes": {"brightness": BRIGHTNESS},
        }
        return Response(
            200, "OK", Headers({"Content-Type": "application/json"}), json.dumps(data).encode()
        )
    return None


def websocket(socket):
    socket.send(json.dumps({"type": "auth_required"}))
    auth = json.loads(socket.recv(timeout=5))
    if auth != {"type": "auth", "access_token": TOKEN}:
        socket.send(json.dumps({"type": "auth_invalid"}))
        return
    socket.send(json.dumps({"type": "auth_ok"}))
    results = {
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
                "platform": "other",
                "translation_key": "hue_light",
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
    global BRIGHTNESS
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
            "attributes": {"brightness": BRIGHTNESS},
        }
        BRIGHTNESS = 180 if BRIGHTNESS == 120 else 120
        new = old | {"attributes": {"brightness": BRIGHTNESS}, "last_updated": fired}
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
        for index in range(2):
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
            if index == 0:
                command = json.loads(socket.recv(timeout=5))


if __name__ == "__main__":
    with serve(websocket, "0.0.0.0", 8123, process_request=http_response) as server:
        server.serve_forever()
