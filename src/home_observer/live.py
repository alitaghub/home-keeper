"""Live collector process: credentials remain here, outside API and dashboard."""

import logging
import os
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread

import httpx

from home_observer.home_assistant import ConnectorError, HomeAssistantConnector
from home_observer.runtime import load_token


def _health(client: httpx.Client, error: str | None):
    client.post(
        "/internal/health",
        json={
            "source_id": "home_assistant",
            "status": "failed" if error else "healthy",
            "received_at": datetime.now(UTC).isoformat(),
            "error": error,
        },
    ).raise_for_status()


def _failure_reason(error):
    if isinstance(error, ConnectorError):
        return error.code
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403}:
        return "authentication"
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return "invalid_data"
    return "ingestion"


MAX_BUFFER_EVENTS = 256
MAX_BUFFER_BYTES = 1024 * 1024


class _EventBuffer:
    """One bounded reader drains the selected stream while HTTP snapshots run."""

    def __init__(self, subscription, max_events, max_bytes):
        self.subscription = subscription
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._queue = deque()
        self._bytes = 0
        self._failure = None
        self._lock = Lock()
        self._stop = Event()
        self._reader = Thread(target=self._read, name="selected-event-buffer", daemon=True)
        self._reader.start()

    def _read(self):
        while not self._stop.is_set():
            try:
                event = self.subscription.receive(0.1)
                if event is None:
                    self._stop.wait(0.005)
                    continue
                size = len(event.model_dump_json().encode())
                with self._lock:
                    if len(self._queue) >= self._max_events or self._bytes + size > self._max_bytes:
                        self._failure = "overflow"
                        return
                    self._queue.append((event, size))
                    self._bytes += size
            except ConnectorError as error:
                with self._lock:
                    self._failure = error.code
                return

    def check(self):
        with self._lock:
            if self._failure:
                raise ConnectorError(self._failure)

    def drain(self):
        with self._lock:
            if self._failure:
                raise ConnectorError(self._failure)
            events = [event for event, _ in self._queue]
            self._queue.clear()
            self._bytes = 0
        return events

    def close(self):
        self._stop.set()
        self.subscription.close()
        self._reader.join(timeout=6)
        if self._reader.is_alive():
            raise ConnectorError("timeout")


class CollectionSession:
    """Owns subscription continuity and proves persistence before closing coverage gaps."""

    def __init__(
        self, client, connector, *, max_events=MAX_BUFFER_EVENTS, max_bytes=MAX_BUFFER_BYTES
    ):
        if not 1 <= max_events <= MAX_BUFFER_EVENTS or not 1 <= max_bytes <= MAX_BUFFER_BYTES:
            raise ValueError("Invalid recovery buffer limits")
        self.client = client
        self.connector = connector
        self._buffer = None
        self._max_events = max_events
        self._max_bytes = max_bytes
        self.last_error = None

    def _coverage(self, status, reason, revision=None):
        response = self.client.post(
            "/internal/coverage",
            json={
                "source_id": "home_assistant",
                "status": status,
                "reason": reason,
                "received_at": datetime.now(UTC).isoformat(),
                "selection_revision": revision,
            },
        )
        if response.status_code == 409:
            raise ConnectorError("selection")
        response.raise_for_status()

    def close(self):
        if self._buffer is not None:
            buffer, self._buffer = self._buffer, None
            buffer.close()

    def interrupt(self, reason):
        self.last_error = reason
        self.close()
        try:
            self._coverage("interrupted", "connection" if reason == "discovery" else reason)
            _health(
                self.client,
                reason if reason not in {"overflow", "ingestion", "selection"} else "connection",
            )
        except (httpx.HTTPError, ConnectorError):
            # The API detects missing heartbeats if it cannot receive our gap record.
            pass

    def _persist(self, path, value):
        response = self.client.post(path, json=value.model_dump(mode="json", exclude_none=True))
        if response.status_code == 409:
            raise ConnectorError("selection")
        response.raise_for_status()

    def receive(self, timeout=0, *, deadline=None):
        if self._buffer is None:
            if timeout:
                time.sleep(timeout)
            return
        if timeout:
            self._buffer._stop.wait(timeout)
        deadline = time.monotonic() + 30 if deadline is None else deadline
        for event in self._buffer.drain():
            if time.monotonic() >= deadline:
                raise ConnectorError("timeout")
            self._persist("/internal/events", event)

    def reconcile(self):
        deadline = time.monotonic() + 30
        try:
            catalog = self.connector.discover()
            self.client.post(
                "/internal/catalog", json=catalog.model_dump(mode="json")
            ).raise_for_status()
            response = self.client.get("/internal/selection")
            response.raise_for_status()
            selection = response.json()
            supported = {entity.id for entity in catalog.entities if entity.supported}
            selected = {
                identity: selection["revision"]
                for identity in selection["entity_ids"]
                if identity in supported
            }
            self.connector.approve(selected)
            key = self.connector.subscription_key
            if self._buffer is not None and self._buffer.subscription.key != key:
                self.interrupt("selection")
            if self._buffer is None and key:
                self._buffer = _EventBuffer(
                    self.connector.subscribe_selected(), self._max_events, self._max_bytes
                )
            for identity, revision in selected.items():
                if time.monotonic() >= deadline:
                    raise ConnectorError("timeout")
                observation = self.connector.read_selected(identity, revision)
                observation.kind = "reconciliation"
                self._persist("/internal/baselines", observation)
            self.receive(deadline=deadline)
            if len(selected) != len(selection["entity_ids"]):
                raise ConnectorError("missing")
            # The API can detect a restart/heartbeat gap independently of this process.
            # Every successful full pass is an explicit, revision-bound recovery proof.
            if self._buffer is not None:
                self._buffer.check()
            self._coverage("recovered", "connection", selection["revision"])
            if self._buffer is not None:
                self._buffer.check()
            _health(self.client, None)
            self.last_error = None
            return True
        except (ConnectorError, httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            reason = _failure_reason(error)
            self.interrupt(reason)
            return False


def reconcile(client: httpx.Client, connector: HomeAssistantConnector, session=None):
    if session is None:
        session = getattr(connector, "_collection_session", None)
        if session is None:
            session = CollectionSession(client, connector)
            connector._collection_session = session
    return session.reconcile()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Library debug output can include request headers or authentication frames.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    service_token_path = Path(os.environ.get("OBSERVER_TOKEN_FILE", "/secrets/service-token"))
    while not service_token_path.exists():
        time.sleep(1)
    try:
        token = load_token(service_token_path)
        if not token or not token.isascii() or any(not 33 <= ord(c) <= 126 for c in token):
            raise ValueError
    except (OSError, ValueError):
        logging.error("Live collector internal credential invalid; check service token file")
        raise SystemExit(1) from None
    with httpx.Client(
        base_url="http://api:8000",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            interval = int(os.environ.get("OBSERVER_RECONCILIATION_SECONDS", "10"))
            if not 1 <= interval <= 3600:
                raise ValueError
            connector = HomeAssistantConnector(
                os.environ.get("OBSERVER_HA_URL", ""),
                Path(os.environ.get("OBSERVER_HA_TOKEN_FILE", "/run/secrets/ha-token")),
                redaction_values=(token,),
            )
        except (ConnectorError, ValueError):
            logging.error("Home Assistant configuration invalid; check origin and token file")
            try:
                _health(client, "configuration")
            except httpx.HTTPError:
                pass
            raise SystemExit(1) from None
        session = CollectionSession(client, connector)
        backoff = 1.0
        next_pass = 0.0
        try:
            while True:
                try:
                    if time.monotonic() >= next_pass:
                        successful = session.reconcile()
                        if session.last_error in {"authentication", "configuration"}:
                            logging.error(
                                "Collection halted; fix local credentials/configuration and restart"
                            )
                            raise SystemExit(1)
                        delay = interval if successful else min(60, backoff)
                        backoff = 1.0 if successful else min(60, backoff * 2)
                        next_pass = time.monotonic() + delay
                    session.receive(min(0.1, max(0.01, next_pass - time.monotonic())))
                except (ConnectorError, httpx.HTTPError) as error:
                    reason = _failure_reason(error)
                    session.interrupt(reason)
                    if reason in {"authentication", "configuration"}:
                        logging.error(
                            "Collection halted; fix local credentials/configuration and restart"
                        )
                        raise SystemExit(1) from None
                    next_pass = time.monotonic() + min(60, backoff)
                    backoff = min(60, backoff * 2)
        finally:
            session.close()
            connector.close()


if __name__ == "__main__":
    main()
