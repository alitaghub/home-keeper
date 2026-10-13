"""Independent public-boundary checks for coverage evidence truthfulness."""

from datetime import timedelta

from fastapi.testclient import TestClient
from test_baselines import observation, select
from test_discovery import AUTH, CATALOG, NOW, TOKEN
from test_events import event

from home_observer.api import create_app


def post(client, path, value):
    response = client.post(path, json=value, headers=AUTH)
    assert response.is_success, response.text
    return response.json()


def gap(clock, revision, **changes):
    return {
        "source_id": "demo",
        "status": "interrupted",
        "reason": "connection",
        "received_at": clock.isoformat(),
        "selection_revision": revision,
        **changes,
    }


def test_scoped_gap_does_not_borrow_another_lights_confirmation(tmp_path):
    clock = [NOW]
    with TestClient(create_app(tmp_path / "scope.sqlite", TOKEN, now=lambda: clock[0])) as client:
        post(client, "/internal/catalog", CATALOG)
        select(client)
        revision = select(client, "bulb-b")
        post(client, "/internal/baselines", observation(revision))
        clock[0] += timedelta(seconds=5)
        post(
            client,
            "/internal/baselines",
            observation(revision, clock[0].isoformat(), entity_id="bulb-b"),
        )
        clock[0] += timedelta(seconds=1)
        post(client, "/internal/coverage", gap(clock[0], revision, entity_id="bulb-a"))
        evidence = client.get("/api/coverage").json()["gaps"][0]
        assert evidence["entity_ids"] == ["bulb-a"]
        assert evidence["last_confirmed_at"] == NOW.isoformat()


def test_restart_gap_survives_retention_and_recovery_cannot_invent_outage(tmp_path):
    clock = [NOW]
    path = tmp_path / "restart.sqlite"

    def app():
        return create_app(path, TOKEN, now=lambda: clock[0], retention_days=1)

    with TestClient(app()) as client:
        post(client, "/internal/catalog", CATALOG)
        revision = select(client)
        post(client, "/internal/baselines", observation(revision))
    clock[0] += timedelta(days=2)
    with TestClient(app()) as client:
        evidence = client.get("/api/coverage").json()["gaps"][0]
        assert evidence["last_confirmed_at"] == NOW.isoformat()
        assert evidence["reason"] == "restart" and evidence["uncertain_start"]
        assert client.get("/api/baselines?entity_id=bulb-a").json()["baselines"] == []
        assert client.get("/api/monitored").json()["entities"][0]["baseline"]["state"] == "on"
        recovered = post(
            client,
            "/internal/baselines",
            observation(revision, clock[0].isoformat(), state="off"),
        )["baseline"]
        assert recovered["kind"] == "recovery"
        assert recovered["coverage_gap_ids"] == [evidence["id"]]
        assert recovered["transition_time"] == "unknown"
        post(client, "/internal/coverage", gap(clock[0], revision, status="recovered"))
        assert client.get("/api/events").json()["events"] == []
        assert client.get("/api/monitored").json()["entities"][0]["baseline"]["state"] == "off"


def test_delayed_pre_gap_event_does_not_overwrite_recovery_snapshot(tmp_path):
    clock = [NOW]
    with TestClient(create_app(tmp_path / "order.sqlite", TOKEN, now=lambda: clock[0])) as client:
        post(client, "/internal/catalog", CATALOG)
        revision = select(client)
        post(client, "/internal/baselines", observation(revision, last_updated=NOW.isoformat()))
        clock[0] += timedelta(seconds=1)
        post(client, "/internal/coverage", gap(clock[0], revision))
        clock[0] += timedelta(seconds=1)
        post(
            client,
            "/internal/baselines",
            observation(
                revision, clock[0].isoformat(), state="off", last_updated=clock[0].isoformat()
            ),
        )
        post(
            client,
            "/internal/events",
            event(revision, "delayed", received_at=clock[0].isoformat()),
        )
        assert client.get("/api/monitored").json()["entities"][0]["baseline"]["state"] == "off"
        assert client.get("/api/events").json()["events"][0]["id"] == "delayed"
        assert client.get("/api/coverage").json()["state"] == "interrupted"
