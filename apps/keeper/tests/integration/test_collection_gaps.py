"""Normal collection through the Home Assistant wire must not invent coverage gaps."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread

from fastapi.testclient import TestClient
from support.home_assistant import Frame
from websockets.datastructures import Headers
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

from home_keeper.api import create_app
from home_keeper.home_assistant import HomeAssistantConnector, SubscriptionKey
from home_keeper.live import CollectionSession
from home_keeper.models import Catalog, ChangeEvent, Entity, LightState, Observation

REGISTRY = [{"id": "a", "entity_id": "light.a", "platform": "hue", "translation_key": "hue_light"}]


def light(brightness: int, updated: str) -> Frame:
    return {
        "entity_id": "light.a",
        "state": "on",
        "attributes": {"brightness": brightness},
        "last_updated": updated,
    }


def test_snapshot_read_after_buffered_event_does_not_record_gap(tmp_path: Path) -> None:
    token = tmp_path / "ha-token"
    token.write_text("synthetic-ha-token")
    current = [light(100, "2025-01-01T10:00:00Z")]
    change_during_read = [False]
    emit = Event()
    finish = Event()

    def request(_connection: ServerConnection, incoming: Request) -> Response | None:
        if not incoming.path.startswith("/api/states/"):
            return None
        if change_during_read[0]:
            current[0] = light(180, "2025-01-01T10:00:01Z")
            emit.set()
            # Home Assistant fires the event before this read returns; let the buffer queue it.
            time.sleep(0.5)
        body = json.dumps(current[0]).encode()
        return Response(200, "OK", Headers({"Content-Type": "application/json"}), body)

    def handler(socket: ServerConnection) -> None:
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
                        headers={"X-Home-Keeper": "1"},
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


class QueuedSubscription:
    def __init__(self, key: SubscriptionKey) -> None:
        self.key = key
        self.pending: Queue[ChangeEvent] = Queue()
        self.taken = Event()

    def receive(self, timeout: float = 1) -> ChangeEvent | None:
        try:
            event = self.pending.get(timeout=timeout)
        except Empty:
            return None
        self.taken.set()
        return event

    def close(self) -> None:
        pass


class EventAfterReadConnector:
    """Home Assistant delivers a change after a state read returned, before it is posted."""

    subscription_key: SubscriptionKey = (("a", 1, "light.a"),)

    def __init__(self) -> None:
        self.subscription = QueuedSubscription(self.subscription_key)
        self.event_after_read = False

    def discover(self) -> Catalog:
        return Catalog(
            source_id="home_assistant",
            entities=[Entity(id="ha:a", name="Light", supported=True)],
        )

    def approve(self, selected: dict[str, int]) -> None:
        pass

    def subscribe_selected(self) -> QueuedSubscription:
        return self.subscription

    def read_selected(self, identity: str, revision: int) -> Observation:
        started_at = datetime.now(UTC)
        observation = Observation(
            state="on",
            availability="available",
            brightness=100,
            last_updated=datetime(2025, 1, 1, 10, tzinfo=UTC),
            entity_id=identity,
            source_entity_id="light.a",
            source_id="home_assistant",
            selection_revision=revision,
            received_at=datetime.now(UTC),
            collection_started_at=started_at,
        )
        if self.event_after_read:
            changed_at = datetime.now(UTC)
            self.subscription.pending.put(
                ChangeEvent(
                    id="change-after-read",
                    entity_id=identity,
                    source_entity_id="light.a",
                    source_id="home_assistant",
                    selection_revision=revision,
                    event_time=changed_at,
                    received_at=changed_at,
                    old=LightState(state="on", brightness=100),
                    new=LightState(state="on", brightness=180),
                )
            )
            assert self.subscription.taken.wait(5)
            # Let the buffer thread queue the event so the post-read drain posts it first.
            time.sleep(0.05)
        return observation

    def close(self) -> None:
        pass


def test_event_received_after_read_keeps_pass_and_recovers_gap(tmp_path: Path) -> None:
    connector = EventAfterReadConnector()
    app = create_app(tmp_path / "race.sqlite", "internal-secret", source_id="home_assistant")
    with TestClient(app, headers={"Authorization": "Bearer internal-secret"}) as api:
        session = CollectionSession(api, connector)
        try:
            assert session.reconcile()
            api.put(
                "/api/selection/ha:a", json={"selected": True}, headers={"X-Home-Keeper": "1"}
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
            [seeded] = api.get("/api/coverage").json()["gaps"]

            connector.event_after_read = True
            assert session.reconcile(), session.last_error

            gaps = api.get("/api/coverage").json()["gaps"]
            assert [(gap["id"], gap["recovered_at"] is not None) for gap in gaps] == [
                (seeded["id"], True)
            ]
            [light] = api.get("/api/monitored").json()["entities"]
            assert light["baseline"]["brightness"] == 180
            # The race only happened if the event was saved before the older snapshot.
            [event] = api.get("/api/events").json()["events"]
            recovery = api.get("/api/baselines", params={"entity_id": "ha:a"}).json()["baselines"][
                0
            ]
            assert recovery["kind"] == "recovery"
            assert event["persisted_at"] < recovery["persisted_at"]
        finally:
            session.close()
