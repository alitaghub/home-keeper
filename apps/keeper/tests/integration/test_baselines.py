from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from support.api import (
    AUTH,
    CATALOG,
    NOW,
    app_for,
    baseline_list,
    observation,
    recorded_current,
    select,
)


def test_baseline_is_attributed_retained_and_delayed_observations_cannot_bypass_selection(
    tmp_path: Path,
) -> None:
    db = tmp_path / "history.sqlite"
    with TestClient(app_for(db)) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        revision = select(client)
        accepted = client.post("/internal/baselines", json=observation(revision), headers=AUTH)
        assert accepted.is_success, accepted.text
        records = baseline_list(client)
        assert len(records) == 1
        baseline = records[0]
        assert baseline["id"]
        assert baseline["entity_id"] == "bulb-a"
        assert baseline["source_id"] == "demo"
        assert baseline["state"] == "on"
        assert baseline["availability"] == "available"
        assert baseline["received_at"] != baseline["last_changed"]
        assert datetime.fromisoformat(baseline["persisted_at"]) == NOW
        assert datetime.fromisoformat(baseline["received_at"]) == NOW
        assert baseline_list(client, "bulb-b") == []
        current = select(client, "bulb-b")
        # Adding another light invalidates the collector's former selection snapshot.
        assert client.post(
            "/internal/baselines", json=observation(revision, state="off"), headers=AUTH
        ).is_client_error
        assert baseline_list(client) == records
        removed = select(client, selected=False)
        for stale in (current, removed):
            assert client.post(
                "/internal/baselines", json=observation(stale), headers=AUTH
            ).is_client_error
        for unapproved in ("room", "plug", "unknown"):
            assert client.post(
                "/internal/baselines", json=observation(removed, entity_id=unapproved), headers=AUTH
            ).is_client_error
        assert baseline_list(client) == records
        assert {entry["id"] for entry in client.get("/api/monitored").json()["entities"]} == {
            "bulb-b"
        }
    with TestClient(app_for(db)) as restarted:
        assert baseline_list(restarted) == records
        assert {entry["id"] for entry in restarted.get("/api/monitored").json()["entities"]} == {
            "bulb-b"
        }


def test_unchanged_snapshots_deduplicate_and_invalid_receipts_are_rejected(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    with TestClient(app_for(tmp_path / "freshness.sqlite", now=lambda: clock[0])) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        revision = select(client)
        assert client.post(
            "/internal/baselines", json=observation(revision), headers=AUTH
        ).is_success
        records = baseline_list(client)
        clock[0] = NOW + timedelta(seconds=121)
        assert client.post(
            "/internal/baselines", json=observation(revision, clock[0].isoformat()), headers=AUTH
        ).is_success
        assert baseline_list(client) == records
        for invalid_receipt in ("2026-10-12T10:02:01", "2030-01-01T00:00:00Z"):
            assert client.post(
                "/internal/baselines", json=observation(revision, invalid_receipt), headers=AUTH
            ).is_client_error
        assert baseline_list(client) == records


def test_history_pages_are_bounded_and_retention_filters_before_cleanup(tmp_path: Path) -> None:
    clock = [NOW]
    db = tmp_path / "pages.sqlite"
    with TestClient(app_for(db, now=lambda: clock[0])) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH).raise_for_status()
        revision = select(client)
        for brightness in range(4):
            clock[0] += timedelta(seconds=1)
            client.post(
                "/internal/baselines",
                json=observation(revision, clock[0].isoformat(), brightness=brightness),
                headers=AUTH,
            ).raise_for_status()
        params = {"entity_id": "bulb-a", "limit": 2}
        first = client.get("/api/baselines", params=params).json()
        second = client.get(
            "/api/baselines", params=params | {"cursor": first["next_cursor"]}
        ).json()
        assert [b["brightness"] for b in first["baselines"] + second["baselines"]] == [3, 2, 1, 0]
        assert second["next_cursor"] is None
        assert client.get("/api/baselines", params=params | {"limit": 101}).status_code == 422
        clock[0] += timedelta(days=31)
        assert client.get("/api/baselines", params=params).json()["baselines"] == []
        assert recorded_current(db)["brightness"] == 3


def test_snapshot_older_than_previous_snapshot_is_rejected(tmp_path: Path) -> None:
    with TestClient(app_for(tmp_path / "older.sqlite")) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        revision = select(client)
        assert client.post(
            "/internal/baselines", json=observation(revision), headers=AUTH
        ).is_success
        older = (NOW - timedelta(seconds=1)).isoformat()
        rejected = client.post(
            "/internal/baselines",
            json=observation(revision, received_at=older, state="off"),
            headers=AUTH,
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "observation_older"
        assert [record["state"] for record in baseline_list(client)] == ["on"]
