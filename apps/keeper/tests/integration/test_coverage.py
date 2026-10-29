"""Coverage gaps: interruptions are recorded truthfully and recovery is never invented."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from support.api import (
    AUTH,
    CATALOG,
    NOW,
    TOKEN,
    Payload,
    catalog_reader,
    event,
    observation,
    recorded_current,
    select,
)

from home_keeper.api import create_app


def coverage(clock: datetime, status: str = "interrupted", **changes: object) -> Payload:
    return {
        "source_id": "demo",
        "status": status,
        "reason": "connection",
        "received_at": clock.isoformat(),
        "selection_revision": 1,
    } | changes


def gap(clock: datetime, revision: int, **changes: object) -> Payload:
    return {
        "source_id": "demo",
        "status": "interrupted",
        "reason": "connection",
        "received_at": clock.isoformat(),
        "selection_revision": revision,
        **changes,
    }


def post(client: TestClient, path: str, value: Payload) -> Any:
    response = client.post(path, json=value, headers=AUTH)
    assert response.is_success, response.text
    return response.json()


def test_failure_recovery_is_explicit_and_never_invents_bulb_events(tmp_path: Path) -> None:
    clock = [NOW]
    with TestClient(
        create_app(tmp_path / "db", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post("/internal/baselines", json=observation(revision), headers=AUTH)
        clock[0] += timedelta(seconds=5)
        payload = coverage(clock[0])
        assert client.post("/internal/coverage", json=payload, headers=AUTH).is_success
        client.post("/internal/coverage", json=payload, headers=AUTH)
        result = client.get("/api/coverage").json()
        assert result["state"] == "interrupted"
        assert len(result["gaps"]) == 1
        assert result["gaps"][0]["last_confirmed_at"] == NOW.isoformat()
        assert result["gaps"][0]["uncertain_start"] is True
        client.post(
            "/internal/health",
            json={"source_id": "demo", "status": "healthy", "received_at": clock[0].isoformat()},
            headers=AUTH,
        )
        assert client.get("/api/coverage").json()["state"] == "interrupted"
        snapshot = observation(
            revision, received_at=clock[0].isoformat(), state="off", kind="recovery"
        )
        recorded = client.post("/internal/baselines", json=snapshot, headers=AUTH).json()[
            "baseline"
        ]
        assert recorded["kind"] == "recovery"
        assert recorded["coverage_gap_ids"]
        assert client.post(
            "/internal/coverage", json=coverage(clock[0], "recovered"), headers=AUTH
        ).is_success
        assert client.get("/api/coverage").json()["state"] == "collecting"
        assert client.get("/api/events").json()["events"] == []


def test_restart_heartbeat_retention_and_scoped_access(tmp_path: Path) -> None:
    clock = [NOW]
    path = tmp_path / "db"

    def app() -> FastAPI:
        return create_app(
            path,
            TOKEN,
            now=lambda: clock[0],
            reconciliation_seconds=10,
            retention_days=1,
            reader=catalog_reader(),
        )

    with TestClient(app()) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post("/internal/baselines", json=observation(revision), headers=AUTH)
        client.post(
            "/internal/health",
            json={"source_id": "demo", "status": "healthy", "received_at": clock[0].isoformat()},
            headers=AUTH,
        )
        clock[0] += timedelta(seconds=21)
        assert client.get("/api/coverage").json()["gaps"][0]["reason"] == "heartbeat"
        assert (
            client.post(
                "/internal/coverage", json=coverage(clock[0], entity_id="bulb-b"), headers=AUTH
            ).status_code
            == 409
        )
        assert client.post("/internal/coverage", json=coverage(clock[0])).status_code == 401
        assert (
            client.post(
                "/internal/coverage", json=coverage(clock[0], reason="arbitrary")
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/internal/coverage", json=coverage(clock[0], reason="arbitrary"), headers=AUTH
            ).status_code
            == 422
        )
        assert client.get("/api/coverage?limit=101").status_code == 422
        client.post("/internal/coverage", json=coverage(clock[0], "recovered"), headers=AUTH)
    clock[0] += timedelta(seconds=1)
    with TestClient(app()) as client:
        gaps = client.get("/api/coverage").json()["gaps"]
        assert any(gap["reason"] == "restart" and gap["recovered_at"] is None for gap in gaps)
        clock[0] += timedelta(days=2)
        gaps = client.get("/api/coverage").json()["gaps"]
        assert len(gaps) == 1
        assert gaps[0]["recovered_at"] is None
        assert client.get("/api/events").json()["events"] == []


def test_reconciliation_mismatch_is_a_gap_and_snapshot_not_fabricated_event(tmp_path: Path) -> None:
    clock = [NOW]
    with TestClient(
        create_app(tmp_path / "db", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post("/internal/baselines", json=observation(revision), headers=AUTH)
        clock[0] += timedelta(seconds=5)
        response = client.post(
            "/internal/baselines",
            json=observation(
                revision, received_at=clock[0].isoformat(), state="off", kind="reconciliation"
            ),
            headers=AUTH,
        )
        assert response.is_success
        gap = client.get("/api/coverage").json()["gaps"][0]
        assert gap["reason"] == "reconciliation"
        assert gap["entity_ids"] == ["bulb-a"]
        assert response.json()["baseline"]["kind"] == "recovery"
        assert client.get("/api/events").json()["events"] == []
        assert recorded_current(tmp_path / "db")["state"] == "off"


def test_failure_keeps_last_healthy_point_and_delayed_recovery_cannot_erase_new_gap(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    with TestClient(
        create_app(tmp_path / "db", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        select(client)
        client.post(
            "/internal/health",
            json={"source_id": "demo", "status": "healthy", "received_at": clock[0].isoformat()},
            headers=AUTH,
        )
        clock[0] += timedelta(seconds=5)
        client.post(
            "/internal/health",
            json={
                "source_id": "demo",
                "status": "failed",
                "error": "connection",
                "received_at": clock[0].isoformat(),
            },
            headers=AUTH,
        )
        gap = client.get("/api/coverage").json()["gaps"][0]
        assert gap["last_confirmed_at"] == NOW.isoformat()
        assert (
            client.post(
                "/internal/coverage", json=coverage(NOW, "recovered"), headers=AUTH
            ).status_code
            == 409
        )
        assert client.get("/api/coverage").json()["state"] == "interrupted"
        # A repeated failure must retain the same gap's original evidence boundary.
        clock[0] += timedelta(seconds=2)
        client.post(
            "/internal/health",
            json={
                "source_id": "demo",
                "status": "failed",
                "error": "connection",
                "received_at": clock[0].isoformat(),
            },
            headers=AUTH,
        )
        assert client.get("/api/coverage").json()["gaps"][0] == gap


def test_recovery_revision_guard_and_snapshot_event_race(tmp_path: Path) -> None:
    clock = [NOW]
    with TestClient(
        create_app(tmp_path / "db", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post("/internal/baselines", json=observation(revision), headers=AUTH)
        client.post("/internal/events", json=event(revision), headers=AUTH)
        clock[0] += timedelta(seconds=5)
        client.post("/internal/coverage", json=coverage(clock[0]), headers=AUTH)
        clock[0] += timedelta(seconds=1)
        # Source time moved backwards, but the recovered snapshot supersedes pre-gap cache.
        snapshot = observation(revision, received_at=clock[0].isoformat(), state="off")
        assert client.post("/internal/baselines", json=snapshot, headers=AUTH).is_success
        assert recorded_current(tmp_path / "db")["state"] == "off"
        # An event arriving during reconciliation cannot be rolled backwards by a snapshot.
        payload = event(
            revision,
            identity="racing",
            event_time=clock[0].isoformat(),
            received_at=clock[0].isoformat(),
        )
        client.post("/internal/events", json=payload, headers=AUTH)
        assert client.post("/internal/baselines", json=snapshot, headers=AUTH).is_success
        assert recorded_current(tmp_path / "db")["state"] == "on"
        new_revision = select(client, "bulb-b")
        assert (
            client.post(
                "/internal/coverage",
                json=coverage(clock[0], "recovered", selection_revision=revision),
                headers=AUTH,
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/internal/coverage",
                json=coverage(clock[0], "recovered", selection_revision=None),
                headers=AUTH,
            ).status_code
            == 409
        )
        assert client.post(
            "/internal/coverage",
            json=coverage(clock[0], "recovered", selection_revision=new_revision),
            headers=AUTH,
        ).is_success


def test_scoped_gap_does_not_borrow_another_lights_confirmation(tmp_path: Path) -> None:
    clock = [NOW]
    with TestClient(
        create_app(tmp_path / "scope.sqlite", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
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


def test_restart_gap_survives_retention_and_recovery_cannot_invent_outage(tmp_path: Path) -> None:
    clock = [NOW]
    path = tmp_path / "restart.sqlite"

    def app() -> FastAPI:
        return create_app(
            path, TOKEN, now=lambda: clock[0], retention_days=1, reader=catalog_reader()
        )

    with TestClient(app()) as client:
        post(client, "/internal/catalog", CATALOG)
        revision = select(client)
        post(client, "/internal/baselines", observation(revision))
    clock[0] += timedelta(days=2)
    with TestClient(app()) as client:
        evidence = client.get("/api/coverage").json()["gaps"][0]
        assert evidence["last_confirmed_at"] == NOW.isoformat()
        assert evidence["reason"] == "restart"
        assert evidence["uncertain_start"]
        assert client.get("/api/baselines?entity_id=bulb-a").json()["baselines"] == []
        assert recorded_current(tmp_path / "restart.sqlite")["state"] == "on"
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
        assert recorded_current(tmp_path / "restart.sqlite")["state"] == "off"


def test_delayed_pre_gap_event_does_not_overwrite_recovery_snapshot(tmp_path: Path) -> None:
    clock = [NOW]
    with TestClient(
        create_app(tmp_path / "order.sqlite", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
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
        assert recorded_current(tmp_path / "order.sqlite")["state"] == "off"
        assert client.get("/api/events").json()["events"][0]["id"] == "delayed"
        assert client.get("/api/coverage").json()["state"] == "interrupted"
