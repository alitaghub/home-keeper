"""Synthetic Home Assistant wire fixture for hardware-free deployment checks."""

import json

from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync.server import serve

TOKEN = "synthetic-compose-token"


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
            "attributes": {"brightness": 120},
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
    for _ in range(2):
        command = json.loads(socket.recv(timeout=5))
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


if __name__ == "__main__":
    with serve(websocket, "0.0.0.0", 8123, process_request=http_response) as server:
        server.serve_forever()
