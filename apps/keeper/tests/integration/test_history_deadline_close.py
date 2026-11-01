"""A history read against a Home Assistant that stalls must end by HISTORY_SECONDS, below the
API's wait for the reader, including the WebSocket closing handshake."""

import json
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from websockets.http11 import Request
from websockets.server import ServerProtocol

from home_keeper.home_assistant import HISTORY_SECONDS, ConnectorError, HomeAssistantConnector
from home_keeper.reader import READER_TIMEOUT_SECONDS


def _stalled_server(listener: socket.socket, stop: threading.Event) -> None:
    """Accepts the WebSocket, authenticates, then never reads or answers again: no history
    result and no reply to the client's close frame."""
    conn, _ = listener.accept()
    protocol = ServerProtocol()
    while not protocol.events_received() and not stop.is_set():
        protocol.receive_data(conn.recv(65536))
        events = protocol.events_received()
        if events:
            request = events[0]
            assert isinstance(request, Request)
            response = protocol.accept(request)
            protocol.send_response(response)
            break
    protocol.send_text(json.dumps({"type": "auth_required"}).encode())
    protocol.send_text(json.dumps({"type": "auth_ok"}).encode())
    conn.sendall(b"".join(protocol.data_to_send()))
    stop.wait(60)
    conn.close()


def test_stalled_history_read_fails_within_the_history_deadline(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("token")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()
    thread = threading.Thread(target=_stalled_server, args=(listener, stop), daemon=True)
    thread.start()
    connector = HomeAssistantConnector(f"http://127.0.0.1:{listener.getsockname()[1]}", token)
    end = datetime.now(UTC)
    started = time.monotonic()
    try:
        with pytest.raises(ConnectorError) as error:
            connector.history(["light.bulb"], end - timedelta(days=1), end)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        connector.close()
        listener.close()
    assert error.value.code == "timeout"
    print(
        f"elapsed={elapsed:.2f}s HISTORY_SECONDS={HISTORY_SECONDS} "
        f"READER_TIMEOUT_SECONDS={READER_TIMEOUT_SECONDS}"
    )
    assert elapsed < HISTORY_SECONDS + 1
    assert elapsed < READER_TIMEOUT_SECONDS
