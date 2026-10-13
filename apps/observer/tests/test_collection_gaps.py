"""Normal collection through the Home Assistant wire must not invent coverage gaps."""

import json
import time
from threading import Event, Thread

from fastapi.testclient import TestClient
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync.server import serve

from home_observer.api import create_app
from home_observer.home_assistant import HomeAssistantConnector
from home_observer.live import CollectionSession

REGISTRY = [{"id": "a", "entity_id": "light.a", "platform": "hue", "translation_key": "hue_light"}]


def light(brightness, updated):
    return {
        "entity_id": "light.a",
        "state": "on",
        "attributes": {"brightness": brightness},
        "last_updated": updated,
    }


def test_snapshot_read_after_buffered_event_does_not_record_gap(tmp_path):
    token = tmp_path / "ha-token"
    token.write_text("synthetic-ha-token")
    current = [light(100, "2025-01-01T10:00:00Z")]
    change_during_read = [False]
    emit = Event()
    finish = Event()

    def request(connection, incoming):
        if not incoming.path.startswith("/api/states/"):
            return None
        if change_during_read[0]:
            current[0] = light(180, "2025-01-01T10:00:01Z")
            emit.set()
            # Home Assistant fires the event before this read returns; let the buffer queue it.
            time.sleep(0.5)
        body = json.dumps(current[0]).encode()
        return Response(200, "OK", Headers({"Content-Type": "application/json"}), body)

    def handler(socket):
        socket.send(json.dumps({"type": "auth_required"}))
        socket.recv(timeout=5)
        socket.send(json.dumps({"type": "auth_ok"}))
        command = json.loads(socket.recv(timeout=5))
        if command["type"] != "subscribe_events":
            socket.send(
                json.dumps({"id": 1, "type": "result", "success": True, "result": REGISTRY})
            )
            socket.recv(timeout=5)
            socket.send(json.dumps({"id": 2, "type": "result", "success": True, "result": []}))
            return
        socket.send(json.dumps({"id": 1, "type": "result", "success": True}))
        emit.wait(10)
        if not finish.is_set():
            event = {
                "event_type": "state_changed",
                "time_fired": "2025-01-01T10:00:01Z",
                "data": {
                    "entity_id": "light.a",
                    "old_state": light(100, "2025-01-01T10:00:00Z"),
                    "new_state": current[0],
                },
            }
            socket.send(json.dumps({"id": 1, "type": "event", "event": event}))
        finish.wait(10)

    with serve(handler, "127.0.0.1", 0, process_request=request) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        connector = HomeAssistantConnector(
            f"http://127.0.0.1:{server.socket.getsockname()[1]}", token
        )
        app = create_app(tmp_path / "gaps.sqlite", "internal-secret", source_id="home_assistant")
        try:
            with TestClient(app, headers={"Authorization": "Bearer internal-secret"}) as api:
                session = CollectionSession(api, connector)
                try:
                    assert session.reconcile()
                    api.put(
                        "/api/selection/ha:a",
                        json={"selected": True},
                        headers={"X-Home-Observer": "1"},
                    ).raise_for_status()
                    assert session.reconcile()
                    change_during_read[0] = True
                    assert session.reconcile()
                    events = api.get("/api/events").json()["events"]
                    assert [event["new"]["brightness"] for event in events] == [180]
                    assert api.get("/api/coverage").json()["gaps"] == []
                finally:
                    session.close()
        finally:
            finish.set()
            emit.set()
            connector.close()
            server.shutdown()
            worker.join(timeout=5)
