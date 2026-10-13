"""Selected state_changed subscription against an in-memory websocket."""

from collections import deque
from pathlib import Path

import pytest
from support.home_assistant import FakeSocket, install_socket, registry_entry, state_changed

from home_observer.home_assistant import (
    ConnectorError,
    HomeAssistantConnector,
    SelectedSubscription,
)


def test_subscription_is_selected_only_and_projects_brightness_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text("subscription-secret")
    socket = FakeSocket([registry_entry()])
    install_socket(monkeypatch, socket)
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
                state_changed("light.unselected"),
                state_changed(),
            ]
        )
        stream = connector.subscribe_selected()
        assert stream is not None
        assert socket.sent[-1] == {
            "id": 1,
            "type": "subscribe_events",
            "event_type": "state_changed",
        }
        assert stream.receive(1) is None
        change = stream.receive(1)
        assert change is not None
        assert change.entity_id == "ha:registry1"
        assert change.old is not None
        assert change.new is not None
        assert change.old.brightness == 120
        assert change.new.brightness == 180
        assert "PRIVATE_SENTINEL" not in change.model_dump_json()
        assert "subscription-secret" not in change.model_dump_json()
        assert change.event_time.isoformat() == "2026-10-13T10:00:00+00:00"
        assert change.selection_revision == 1
        stream.close()
    finally:
        connector.close()


def test_malformed_selected_event_is_safe_and_unselected_payload_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text("stream-error-secret")
    socket = FakeSocket([registry_entry()])
    install_socket(monkeypatch, socket)
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
        assert stream is not None
        bad = state_changed(state="stream-error-secret")
        socket.messages = deque([bad, state_changed()])
        with pytest.raises(ConnectorError) as error:
            stream.receive(1)
        assert error.value.code == "invalid_data"
        assert "stream-error-secret" not in str(error.value)
        change = stream.receive(1)
        assert change is not None
        assert change.new is not None
        assert change.new.brightness == 180
        connector.approve({"ha:registry1": 2})
        assert stream.receive(1) is None
        stream.close()
    finally:
        connector.close()
