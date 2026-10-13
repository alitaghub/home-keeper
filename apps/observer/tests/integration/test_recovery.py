"""Recovery proof through the HA wire and trusted API request boundary."""

import json
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from support.home_assistant import FakeSocket, Frame, install_socket, registry_entry
from websockets.datastructures import Headers
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

from home_observer import live
from home_observer.api import create_app
from home_observer.home_assistant import HomeAssistantConnector
from home_observer.live import CollectionSession

# Requests the mocked API accepted, as (path, decoded JSON body).
type Accepted = list[tuple[str, Any]]
type Home = tuple[HomeAssistantConnector, list[str], list[Frame]]


def registry(identity: str = "a", source: str = "light.a") -> Frame:
    return {"id": identity, "entity_id": source, "platform": "hue", "translation_key": "hue_light"}


def state(
    brightness: int, timestamp: str = "2026-10-13T10:00:00Z", source: str = "light.a"
) -> Frame:
    return {
        "entity_id": source,
        "state": "on",
        "attributes": {"brightness": brightness},
        "last_updated": timestamp,
    }


def message(brightness: int) -> Frame:
    return {
        "id": 1,
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": "2026-10-13T10:00:01Z",
            "data": {
                "entity_id": "light.a",
                "old_state": state(100),
                "new_state": state(brightness, "2026-10-13T10:00:01Z"),
            },
        },
    }


@contextmanager
def home(
    tmp_path: Path,
    race_count: int = 1,
    authentication_invalid: bool | Callable[[], bool] = False,
) -> Generator[Home]:
    token = tmp_path / "ha-token"
    token.write_text("private-recovery-token")
    subscribed = Event()
    race_sent = Event()
    finish = Event()
    commands: list[str] = []
    rows = [registry()]

    def request(_connection: ServerConnection, incoming: Request) -> Response | None:
        if incoming.path.startswith("/api/states/"):
            # A snapshot cannot begin before the event subscription is acknowledged.
            assert subscribed.wait(1)
            race_sent.set()
            time.sleep(0.05)
            return Response(
                200,
                "OK",
                Headers({"Content-Type": "application/json"}),
                json.dumps(state(100, source=incoming.path.rsplit("/", 1)[-1])).encode(),
            )
        return None

    def handler(socket: ServerConnection) -> None:
        socket.send(json.dumps({"type": "auth_required"}))
        auth = json.loads(socket.recv(timeout=5))
        assert auth["access_token"] == "private-recovery-token"
        invalid = (
            authentication_invalid() if callable(authentication_invalid) else authentication_invalid
        )
        socket.send(json.dumps({"type": "auth_invalid" if invalid else "auth_ok"}))
        if invalid:
            return
        command = json.loads(socket.recv(timeout=5))
        commands.append(command["type"])
        if command["type"] == "subscribe_events":
            race_sent.clear()
            socket.send(json.dumps({"id": 1, "type": "result", "success": True}))
            subscribed.set()
            assert race_sent.wait(3)
            for number in range(race_count):
                socket.send(json.dumps(message(180 + number % 20)))
            finish.wait(3)
        else:
            socket.send(json.dumps({"id": 1, "type": "result", "success": True, "result": rows}))
            command = json.loads(socket.recv(timeout=5))
            commands.append(command["type"])
            socket.send(json.dumps({"id": 2, "type": "result", "success": True, "result": []}))

    with serve(handler, "127.0.0.1", 0, process_request=request) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        connector = HomeAssistantConnector(
            f"http://127.0.0.1:{server.socket.getsockname()[1]}", token
        )
        try:
            yield connector, commands, rows
        finally:
            finish.set()
            connector.close()
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def test_snapshot_race_is_persisted_before_recovery_and_subscription_survives_poll(
    tmp_path: Path,
) -> None:
    accepted: Accepted = []

    def api(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        if request.url.path == "/internal/selection":
            return httpx.Response(200, json={"revision": 1, "entity_ids": ["ha:a"]})
        if request.url.path == "/internal/baselines":
            time.sleep(0.05)
        accepted.append((request.url.path, payload))
        return httpx.Response(200, json={})

    with (
        home(tmp_path) as (connector, commands, _),
        httpx.Client(base_url="http://api", transport=httpx.MockTransport(api)) as client,
    ):
        session = CollectionSession(client, connector)
        try:
            assert session.reconcile()
            assert any(
                path == "/internal/events" and value["new"]["brightness"] == 180
                for path, value in accepted
            )
            paths = [path for path, _ in accepted]
            assert paths.index("/internal/events") < paths.index("/internal/coverage")
            assert session.reconcile()
            assert commands.count("subscribe_events") == 1
            wire = json.dumps(accepted)
            assert "private-recovery-token" not in wire
        finally:
            session.close()


@pytest.mark.parametrize("failed_path", ["/internal/baselines", "/internal/events"])
def test_failed_ingestion_never_reports_healthy_and_resynchronizes(
    tmp_path: Path, failed_path: str
) -> None:
    accepted: Accepted = []
    fail = [True]

    def api(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        if request.url.path == "/internal/selection":
            return httpx.Response(200, json={"revision": 1, "entity_ids": ["ha:a"]})
        accepted.append((request.url.path, payload))
        if request.url.path == failed_path and fail[0]:
            return httpx.Response(503)
        return httpx.Response(200, json={})

    with (
        home(tmp_path) as (connector, commands, _),
        httpx.Client(base_url="http://api", transport=httpx.MockTransport(api)) as client,
    ):
        session = CollectionSession(client, connector)
        try:
            assert not session.reconcile()
            assert session.last_error == "ingestion"
            assert not any(
                path == "/internal/health" and value["status"] == "healthy"
                for path, value in accepted
            )
            assert any(
                path == "/internal/coverage"
                and value["reason"] == "ingestion"
                and value["status"] == "interrupted"
                for path, value in accepted
            )
            assert not any(
                path == "/internal/coverage" and value["status"] == "recovered"
                for path, value in accepted
            )
            fail[0] = False
            assert session.reconcile()
            assert commands.count("subscribe_events") == 2
            assert accepted[-1][1]["status"] == "healthy"
        finally:
            session.close()


@pytest.mark.parametrize("limits", [{"max_events": 2}, {"max_bytes": 100}])
def test_bounded_buffer_overflow_marks_incomplete_and_requires_new_subscription(
    tmp_path: Path, limits: dict[str, int]
) -> None:
    accepted: Accepted = []

    def api(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        if request.url.path == "/internal/selection":
            return httpx.Response(200, json={"revision": 1, "entity_ids": ["ha:a"]})
        if request.url.path == "/internal/baselines":
            time.sleep(0.1)
        accepted.append((request.url.path, payload))
        return httpx.Response(200, json={})

    with (
        home(tmp_path, race_count=4) as (connector, commands, _),
        httpx.Client(base_url="http://api", transport=httpx.MockTransport(api)) as client,
    ):
        session = CollectionSession(client, connector, **limits)
        try:
            assert not session.reconcile()
            assert session.last_error == "overflow"
            assert any(
                path == "/internal/coverage" and value["reason"] == "overflow"
                for path, value in accepted
            )
            assert not any(
                path == "/internal/coverage" and value["status"] == "recovered"
                for path, value in accepted
            )
            assert not any(
                path == "/internal/health" and value["status"] == "healthy"
                for path, value in accepted
            )
            # A fresh attempt installs another subscription rather than accepting the old buffer.
            assert not session.reconcile()
            assert commands.count("subscribe_events") == 2
        finally:
            session.close()


def test_latest_selection_and_registry_identity_are_revalidated_before_state_reads(
    tmp_path: Path,
) -> None:
    accepted: Accepted = []
    selected = {"revision": 1, "entity_ids": ["ha:a"]}

    def api(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        if request.url.path == "/internal/selection":
            return httpx.Response(200, json=selected)
        accepted.append((request.url.path, payload))
        return httpx.Response(200, json={})

    with (
        home(tmp_path) as (connector, _, rows),
        httpx.Client(base_url="http://api", transport=httpx.MockTransport(api)) as client,
    ):
        session = CollectionSession(client, connector)
        try:
            assert session.reconcile()
            session.interrupt("connection")
            accepted.clear()
            # A keeps its identity after renaming. The reused source ID is a new identity.
            rows[:] = [
                registry("a", "light.renamed"),
                registry("replacement", "light.a"),
                registry("c", "light.c"),
            ]
            selected.update(revision=2, entity_ids=["ha:a", "ha:c"])
            assert session.reconcile()
            baselines = [value for path, value in accepted if path == "/internal/baselines"]
            assert {(value["entity_id"], value["source_entity_id"]) for value in baselines} == {
                ("ha:a", "light.renamed"),
                ("ha:c", "light.c"),
            }
            assert all(value["selection_revision"] == 2 for value in baselines)
            assert not any(value.get("entity_id") == "ha:replacement" for _, value in accepted)
            recovered = [
                value
                for path, value in accepted
                if path == "/internal/coverage" and value["status"] == "recovered"
            ]
            assert recovered[0]["selection_revision"] == 2
        finally:
            session.close()


def test_authentication_failure_halts_process_and_retains_safe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted: list[Any] = []
    requests: list[str] = []
    service_token = tmp_path / "service-token"
    service_token.write_text("internal-private-token")
    original_client = httpx.Client

    def api(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.content:
            accepted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    # Forwards httpx.Client's own options for every caller; httpx publishes no typed kwargs.
    def client(**kwargs: Any) -> httpx.Client:
        if kwargs.get("base_url") == "http://api:8000":
            return original_client(**kwargs, transport=httpx.MockTransport(api))
        return original_client(**kwargs)

    with home(tmp_path, authentication_invalid=True) as (connector, _, _):
        # The fake API client matches the default port; a developer's OBSERVER_PORT would bypass it.
        monkeypatch.delenv("OBSERVER_PORT", raising=False)
        monkeypatch.setenv("OBSERVER_TOKEN_FILE", str(service_token))
        monkeypatch.setenv("OBSERVER_HA_TOKEN_FILE", str(tmp_path / "ha-token"))
        monkeypatch.setenv("OBSERVER_HA_URL", connector.origin)
        monkeypatch.setattr(live.httpx, "Client", client)
        with pytest.raises(SystemExit) as result:
            live.main()
        assert result.value.code == 1
        assert requests == ["/internal/coverage", "/internal/health"]
        assert accepted[0]["reason"] == "authentication"
        assert accepted[1]["error"] == "authentication"
        assert accepted[1]["status"] == "failed"
        assert "private-token" not in json.dumps(accepted)


def test_api_detected_gap_closes_after_next_full_pass_without_fabricated_events(
    tmp_path: Path,
) -> None:
    with (
        home(tmp_path, race_count=0) as (connector, _, _),
        TestClient(
            create_app(tmp_path / "recorded.sqlite", "internal-secret", source_id="home_assistant"),
            headers={"Authorization": "Bearer internal-secret"},
        ) as api,
    ):
        session = CollectionSession(api, connector)
        try:
            assert session.reconcile()
            api.put(
                "/api/selection/ha:a", json={"selected": True}, headers={"X-Home-Observer": "1"}
            ).raise_for_status()
            assert session.reconcile()
            api.post(
                "/internal/coverage",
                json={
                    "source_id": "home_assistant",
                    "status": "interrupted",
                    "reason": "heartbeat",
                    "received_at": datetime.now(UTC).isoformat(),
                },
            ).raise_for_status()
            assert api.get("/api/coverage").json()["state"] == "interrupted"
            assert session.reconcile()
            assert api.get("/api/coverage").json()["state"] == "collecting"
            snapshots = api.get("/api/baselines?entity_id=ha:a").json()["baselines"]
            assert any(
                value["kind"] == "recovery"
                and value["coverage_gap_ids"]
                and value["transition_time"] == "unknown"
                for value in snapshots
            )
            assert api.get("/api/events").json()["events"] == []
        finally:
            session.close()


def test_obsolete_revision_does_not_close_gap_and_revalidates_new_selection(tmp_path: Path) -> None:
    accepted: Accepted = []
    selected = {"revision": 1, "entity_ids": ["ha:a"]}
    obsolete = [True]

    def api(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        if request.url.path == "/internal/selection":
            return httpx.Response(200, json=selected)
        accepted.append((request.url.path, payload))
        if request.url.path == "/internal/baselines" and obsolete[0]:
            obsolete[0] = False
            selected.update(revision=2, entity_ids=["ha:c"])
            return httpx.Response(409, json={"detail": {"code": "selection_changed"}})
        return httpx.Response(200, json={})

    with (
        home(tmp_path) as (connector, _, rows),
        httpx.Client(base_url="http://api", transport=httpx.MockTransport(api)) as client,
    ):
        rows.append(registry("c", "light.c"))
        session = CollectionSession(client, connector)
        try:
            assert not session.reconcile()
            assert session.last_error == "selection"
            assert not any(
                path == "/internal/coverage" and value["status"] == "recovered"
                for path, value in accepted
            )
            accepted.clear()
            assert session.reconcile()
            baselines = [value for path, value in accepted if path == "/internal/baselines"]
            assert len(baselines) == 1
            assert baselines[0]["entity_id"] == "ha:c"
            assert baselines[0]["selection_revision"] == 2
        finally:
            session.close()


def test_production_retry_backoff_is_capped_and_auth_failure_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    attempts: list[int] = []
    failures = [0]
    service_token = tmp_path / "service-token"
    service_token.write_text("internal-private-token")
    original_client = httpx.Client

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/selection":
            return httpx.Response(200, json={"revision": 1, "entity_ids": ["ha:a"]})
        if request.url.path == "/internal/catalog":
            attempts.append(round(clock[0]))
        if request.url.path == "/internal/baselines":
            failures[0] += 1
            return httpx.Response(503)
        return httpx.Response(200, json={})

    # Forwards httpx.Client's own options for every caller; httpx publishes no typed kwargs.
    def client(**kwargs: Any) -> httpx.Client:
        if kwargs.get("base_url") == "http://api:8000":
            return original_client(**kwargs, transport=httpx.MockTransport(api))
        return original_client(**kwargs)

    def advance(seconds: float) -> None:
        clock[0] += seconds

    with home(tmp_path, authentication_invalid=lambda: failures[0] >= 10) as (
        connector,
        _,
        _,
    ):
        # The fake API client matches the default port; a developer's OBSERVER_PORT would bypass it.
        monkeypatch.delenv("OBSERVER_PORT", raising=False)
        monkeypatch.setenv("OBSERVER_TOKEN_FILE", str(service_token))
        monkeypatch.setenv("OBSERVER_HA_TOKEN_FILE", str(tmp_path / "ha-token"))
        monkeypatch.setenv("OBSERVER_HA_URL", connector.origin)
        monkeypatch.setattr(live.httpx, "Client", client)
        monkeypatch.setattr(
            live, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=advance)
        )
        with pytest.raises(SystemExit) as result:
            live.main()
        assert result.value.code == 1
        assert attempts == [0, 1, 3, 7, 15, 31, 63, 123, 183, 243]
        assert round(clock[0]) == 303


def test_missing_selection_keeps_healthy_light_subscription_and_scopes_gap(tmp_path: Path) -> None:
    accepted: Accepted = []

    def api(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        if request.url.path == "/internal/selection":
            return httpx.Response(200, json={"revision": 1, "entity_ids": ["ha:a", "ha:gone"]})
        accepted.append((request.url.path, payload))
        return httpx.Response(200, json={})

    with (
        home(tmp_path, race_count=0) as (connector, commands, _),
        httpx.Client(base_url="http://api", transport=httpx.MockTransport(api)) as client,
    ):
        session = CollectionSession(client, connector)
        try:
            # ha:gone is selected but absent from discovery; ha:a stays healthy.
            session.reconcile()
            session.reconcile()
            assert commands.count("subscribe_events") == 1
            interrupted = [
                value
                for path, value in accepted
                if path == "/internal/coverage" and value["status"] == "interrupted"
            ]
            assert interrupted
            assert all(value.get("entity_id") == "ha:gone" for value in interrupted)
        finally:
            session.close()


class LiveSocket(FakeSocket):
    """Keeps answering the validated registry fixture, then idles once subscribed."""

    def send(self, value: str) -> None:
        super().send(value)
        if self.sent[-1]["type"] == "subscribe_events":
            self.messages.clear()
            self.messages.append({"id": 1, "type": "result", "success": True})

    def recv(self, timeout: float | None = None) -> str:
        if not self.messages:
            time.sleep(min(timeout or 0, 0.01))
            raise TimeoutError
        return super().recv(timeout)


def test_initial_missing_baseline_stays_selected_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "token"
    secret.write_text("review-ha-secret")
    install_socket(monkeypatch, FakeSocket([registry_entry()]))
    adapter = HomeAssistantConnector("http://ha.example:8123", secret)
    adapter.discover()
    adapter.approve({"ha:registry1": 1})
    status = [404]
    adapter._http.close()  # pyright: ignore[reportPrivateUsage] -- tests replace the transport
    adapter._http = httpx.Client(  # pyright: ignore[reportPrivateUsage] -- tests replace the transport
        base_url=adapter.origin,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                status[0], json={"entity_id": "light.bulb", "state": "off", "attributes": {}}
            )
        ),
    )

    def connect(*_args: object, **_kwargs: object) -> LiveSocket:
        return LiveSocket([registry_entry()])

    monkeypatch.setattr("home_observer.home_assistant.connect", connect)
    session: CollectionSession | None = None
    try:
        with TestClient(
            create_app(tmp_path / "live.sqlite", "review-internal", source_id="home_assistant"),
            headers={"Authorization": "Bearer review-internal"},
        ) as api:
            session = CollectionSession(api, adapter)
            session.reconcile()
            api.put(
                "/api/selection/ha:registry1",
                json={"selected": True},
                headers={"X-Home-Observer": "1"},
            ).raise_for_status()
            session.reconcile()
            assert api.get("/api/selection").json()["entity_ids"] == ["ha:registry1"]
            assert api.get("/api/health").json()["status"] == "healthy"
            gaps = api.get("/api/coverage").json()
            assert gaps["state"] == "interrupted"
            assert gaps["gaps"][0]["entity_ids"] == ["ha:registry1"]
            monitored = api.get("/api/monitored").json()["entities"][0]
            assert monitored["baseline"] is None
            assert monitored["freshness"] == "pending"
            assert api.get("/api/baselines?entity_id=ha:registry1").json()["baselines"] == []
            status[0] = 200
            session.reconcile()
            assert api.get("/api/health").json()["status"] == "healthy"
            assert api.get("/api/monitored").json()["entities"][0]["freshness"] == "fresh"
            assert len(api.get("/api/baselines?entity_id=ha:registry1").json()["baselines"]) == 1
    finally:
        if session is not None:
            session.close()
        adapter.close()
