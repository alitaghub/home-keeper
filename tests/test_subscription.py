import json

import pytest
from test_home_assistant import Socket, row

from home_observer.home_assistant import (
    ConnectorError,
    HomeAssistantConnector,
    SelectedSubscription,
)


def event(entity="light.bulb", **changes):
    state = {
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


def test_subscription_is_selected_only_and_projects_brightness_changes(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("subscription-secret")
    socket = Socket([row()])
    socket.close = lambda: None
    monkeypatch.setattr("home_observer.home_assistant.connect", lambda *a, **kw: socket)
    connector = HomeAssistantConnector("http://ha.example:8123", token)
    try:
        connector.discover()
        connector.approve({})
        assert connector.subscribe_selected() is None
        with pytest.raises(ConnectorError):
            SelectedSubscription(connector)
        assert socket.sent[-1]["type"] == "config/device_registry/list"
        connector.approve({"ha:registry1": 1})
        socket.messages.extend(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": True, "result": None},
                event("light.unselected"),
                event(),
            ]
        )
        stream = connector.subscribe_selected()
        assert socket.sent[-1] == {
            "id": 1,
            "type": "subscribe_events",
            "event_type": "state_changed",
        }
        assert stream.receive(1) is None
        change = stream.receive(1)
        assert change.entity_id == "ha:registry1"
        assert change.old.brightness == 120 and change.new.brightness == 180
        assert "PRIVATE_SENTINEL" not in change.model_dump_json()
        assert "subscription-secret" not in change.model_dump_json()
        assert change.event_time.isoformat() == "2026-10-13T10:00:00+00:00"
        assert change.selection_revision == 1
        stream.close()
    finally:
        connector.close()


def test_actual_selected_subscription_and_revoked_revision(tmp_path):
    from threading import Thread

    from websockets.sync.server import serve

    token = tmp_path / "token"
    token.write_text("wire-stream-secret")
    commands = []

    def handler(socket):
        socket.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(socket.recv(timeout=5))
        assert auth["access_token"] == "wire-stream-secret"
        socket.send(json.dumps({"type": "auth_ok"}))
        request = json.loads(socket.recv(timeout=5))
        commands.append(request["type"])
        if request["type"] == "subscribe_events":
            assert request["event_type"] == "state_changed"
            socket.send(json.dumps({"id": 1, "type": "result", "success": True, "result": None}))
            socket.send(json.dumps(event("light.unselected", state="PRIVATE_SENTINEL")))
            socket.send(json.dumps(event()))
            from websockets.exceptions import ConnectionClosed

            try:
                socket.recv(timeout=5)
            except ConnectionClosed:
                pass
        else:
            assert request["type"] == "config/entity_registry/list"
            socket.send(json.dumps({"id": 1, "type": "result", "success": True, "result": [row()]}))
            request = json.loads(socket.recv(timeout=5))
            commands.append(request["type"])
            assert request["type"] == "config/device_registry/list"
            socket.send(json.dumps({"id": 2, "type": "result", "success": True, "result": []}))

    with serve(handler, "127.0.0.1", 0) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        connector = HomeAssistantConnector(
            f"http://127.0.0.1:{server.socket.getsockname()[1]}", token
        )
        stream = None
        try:
            connector.discover()
            connector.approve({"ha:registry1": 2})
            stream = connector.subscribe_selected()
            assert stream.receive(1) is None
            received = stream.receive(1)
            assert received.entity_id == "ha:registry1" and received.new.brightness == 180
            assert "PRIVATE_SENTINEL" not in received.model_dump_json()
            assert "wire-stream-secret" not in received.model_dump_json()
            connector.approve({})
            assert stream.receive(1) is None
        finally:
            if stream:
                stream.close()
            connector.close()
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()
    assert commands == [
        "config/entity_registry/list",
        "config/device_registry/list",
        "subscribe_events",
    ]


def test_malformed_selected_event_is_safe_and_unselected_payload_is_dropped(tmp_path, monkeypatch):
    from collections import deque

    token = tmp_path / "token"
    token.write_text("stream-error-secret")
    socket = Socket([row()])
    socket.close = lambda: None
    monkeypatch.setattr("home_observer.home_assistant.connect", lambda *a, **kw: socket)
    connector = HomeAssistantConnector("http://ha.example:8123", token)
    try:
        connector.discover()
        connector.approve({"ha:registry1": 1})
        socket.messages.extend(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": True},
            ]
        )
        stream = connector.subscribe_selected()
        bad = event(state="stream-error-secret")
        socket.messages = deque([bad, event()])
        with pytest.raises(ConnectorError) as error:
            stream.receive(1)
        assert error.value.code == "invalid_data"
        assert "stream-error-secret" not in str(error.value)
        assert stream.receive(1).new.brightness == 180
        connector.approve({"ha:registry1": 2})
        assert stream.receive(1) is None
        stream.close()
    finally:
        connector.close()
