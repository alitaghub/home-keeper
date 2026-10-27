"""Live collector process: credentials remain here, outside API and dashboard."""

import contextlib
import logging
import os
import time
from collections import deque
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from threading import Event, Lock, Thread

import httpx
from pydantic import BaseModel

from home_keeper.home_assistant import (
    VISIBLE_ASCII,
    Connector,
    ConnectorError,
    HomeAssistantConnector,
    Subscription,
)
from home_keeper.models import ChangeEvent, SelectionSnapshot
from home_keeper.runtime import load_token


def _health(client: httpx.Client, error: str | None) -> None:
    response = client.post(
        "/internal/health",
        json={
            "source_id": "home_assistant",
            "status": "failed" if error else "healthy",
            "received_at": datetime.now(UTC).isoformat(),
            "error": error,
        },
    )
    _check_response(response)


def _failure_reason(error: Exception) -> str:
    if isinstance(error, ConnectorError):
        return error.code
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403}:
        return "authentication"
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return "invalid_data"
    return "ingestion"


def _check_response(response: httpx.Response) -> None:
    if response.status_code == HTTPStatus.CONFLICT:
        try:
            code = response.json()["detail"]["code"]
        except (ValueError, KeyError, TypeError):
            code = None
        reasons: dict[object, str] = {
            "selection_changed": "selection",
            "source_mismatch": "configuration",
        }
        raise ConnectorError(reasons.get(code, "invalid_data"))
    response.raise_for_status()


MAX_BUFFER_EVENTS = 256
MAX_BUFFER_BYTES = 1024 * 1024
HEARTBEAT_SECONDS = 10
MAX_INTERVAL_SECONDS = 3600


class _EventBuffer:
    """One bounded reader drains the selected stream while HTTP snapshots run."""

    def __init__(self, subscription: Subscription, max_events: int, max_bytes: int) -> None:
        self.subscription = subscription
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._queue: deque[tuple[ChangeEvent, int]] = deque()
        self._bytes = 0
        self._failure: str | None = None
        self._lock = Lock()
        self._stop = Event()
        self._reader = Thread(target=self._read, name="selected-event-buffer", daemon=True)
        self._reader.start()

    def _read(self) -> None:
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

    def check(self) -> None:
        with self._lock:
            if self._failure:
                raise ConnectorError(self._failure)

    def drain(self) -> list[ChangeEvent]:
        with self._lock:
            if self._failure:
                raise ConnectorError(self._failure)
            events = [event for event, _ in self._queue]
            self._queue.clear()
            self._bytes = 0
        return events

    def wait(self, timeout: float) -> None:
        self._stop.wait(timeout)

    def close(self) -> None:
        self._stop.set()
        self.subscription.close()
        self._reader.join(timeout=6)
        if self._reader.is_alive():
            raise ConnectorError("timeout")


class CollectionSession:
    """Owns subscription continuity and proves persistence before closing coverage gaps."""

    def __init__(
        self,
        client: httpx.Client,
        connector: Connector,
        *,
        max_events: int = MAX_BUFFER_EVENTS,
        max_bytes: int = MAX_BUFFER_BYTES,
    ) -> None:
        if not 1 <= max_events <= MAX_BUFFER_EVENTS or not 1 <= max_bytes <= MAX_BUFFER_BYTES:
            raise ValueError("Invalid recovery buffer limits")
        self.client = client
        self.connector = connector
        self._buffer: _EventBuffer | None = None
        self._max_events = max_events
        self._max_bytes = max_bytes
        self.last_error: str | None = None

    def _coverage(
        self,
        status: str,
        reason: str,
        revision: int | None = None,
        entity_id: str | None = None,
        entity_ids: list[str] | None = None,
    ) -> None:
        response = self.client.post(
            "/internal/coverage",
            json={
                "source_id": "home_assistant",
                "status": status,
                "reason": reason,
                "received_at": datetime.now(UTC).isoformat(),
                "selection_revision": revision,
                "entity_id": entity_id,
                "entity_ids": entity_ids,
            },
        )
        _check_response(response)

    def close(self) -> None:
        if self._buffer is not None:
            buffer, self._buffer = self._buffer, None
            buffer.close()

    def interrupt(self, reason: str) -> None:
        self.last_error = reason
        logging.warning("Collection interrupted: %s", reason)
        self.close()
        try:
            self._coverage("interrupted", "connection" if reason == "discovery" else reason)
            _health(
                self.client,
                reason if reason not in {"overflow", "ingestion", "selection"} else "connection",
            )
        except (httpx.HTTPError, ConnectorError):
            # Log only a fixed operation/reason, never exception text or response bodies.
            logging.warning("Interruption reporting failed: %s", reason)

    def _persist(self, path: str, value: BaseModel) -> None:
        response = self.client.post(path, json=value.model_dump(mode="json", exclude_none=True))
        _check_response(response)

    def receive(self, timeout: float = 0, *, deadline: float | None = None) -> None:
        if self._buffer is None:
            if timeout:
                time.sleep(timeout)
            return
        if timeout:
            self._buffer.wait(timeout)
        deadline = time.monotonic() + 30 if deadline is None else deadline
        for event in self._buffer.drain():
            if time.monotonic() >= deadline:
                raise ConnectorError("timeout")
            self._persist("/internal/events", event)

    def reconcile(self) -> bool:
        deadline = time.monotonic() + 30
        try:
            catalog = self.connector.discover()
            _check_response(
                self.client.post("/internal/catalog", json=catalog.model_dump(mode="json"))
            )
            response = self.client.get("/internal/selection")
            response.raise_for_status()
            selection = SelectionSnapshot.model_validate(response.json())
            supported = {entity.id for entity in catalog.entities if entity.supported}
            selected = {
                identity: selection.revision
                for identity in selection.entity_ids
                if identity in supported
            }
            self.connector.approve(selected)
            key = self.connector.subscription_key
            if self._buffer is not None and self._buffer.subscription.key != key:
                self.interrupt("selection")
            if self._buffer is None and key:
                subscription = self.connector.subscribe_selected()
                # An approved, non-empty key always yields a subscription.
                if subscription is None:
                    raise ConnectorError("configuration")
                self._buffer = _EventBuffer(subscription, self._max_events, self._max_bytes)
            deadline = time.monotonic() + max(30, 5 * len(selected) + 10)
            heartbeat = time.monotonic()
            recovered: list[str] = []
            for identity in set(selection.entity_ids) - supported:
                self._coverage("interrupted", "missing", entity_id=identity)
            for identity, revision in selected.items():
                if time.monotonic() >= deadline:
                    raise ConnectorError("timeout")
                self.receive(deadline=deadline)
                try:
                    observation = self.connector.read_selected(identity, revision)
                except ConnectorError as error:
                    if error.code != "missing":
                        raise
                    self._coverage("interrupted", "missing", entity_id=identity)
                    continue
                # A real event captured during the HTTP read explains the sampled change.
                self.receive(deadline=deadline)
                observation.kind = "reconciliation"
                self._persist("/internal/baselines", observation)
                recovered.append(identity)
                if time.monotonic() - heartbeat >= HEARTBEAT_SECONDS:
                    _health(self.client, None)
                    heartbeat = time.monotonic()
            self.receive(deadline=deadline)
            # The API can detect a restart/heartbeat gap independently of this process.
            # Every successful full pass is an explicit, revision-bound recovery proof.
            if self._buffer is not None:
                self._buffer.check()
            self._coverage("recovered", "connection", selection.revision, entity_ids=recovered)
            if self._buffer is not None:
                self._buffer.check()
            _health(self.client, None)
        except (ConnectorError, httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            reason = _failure_reason(error)
            self.interrupt(reason)
            return False
        else:
            self.last_error = None
            return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Library debug output can include request headers or authentication frames.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    service_token_path = Path(os.environ.get("HOME_KEEPER_TOKEN_FILE", "/secrets/service-token"))
    while not service_token_path.exists():
        time.sleep(1)
    try:
        token = load_token(service_token_path)
        if not token or not token.isascii() or any(ord(c) not in VISIBLE_ASCII for c in token):
            raise ValueError
    except (OSError, ValueError):
        logging.error("Live collector internal credential invalid; check service token file")
        raise SystemExit(1) from None
    with httpx.Client(
        base_url=f"http://api:{int(os.environ.get('HOME_KEEPER_PORT', '8000'))}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            interval = int(os.environ.get("HOME_KEEPER_RECONCILIATION_SECONDS", "10"))
            if not 1 <= interval <= MAX_INTERVAL_SECONDS:
                raise ValueError
            connector = HomeAssistantConnector(
                os.environ.get("HOME_KEEPER_HA_URL", ""),
                Path(os.environ.get("HOME_KEEPER_HA_TOKEN_FILE", "/run/secrets/ha-token")),
                redaction_values=(token,),
            )
        except (ConnectorError, ValueError):
            logging.error("Home Assistant configuration invalid; check origin and token file")
            with contextlib.suppress(httpx.HTTPError):
                _health(client, "configuration")
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
