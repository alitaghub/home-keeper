from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from test_discovery import AUTH, BROWSER, CATALOG, NOW, app_for


def observation(revision, received_at=NOW.isoformat(), **changes):
    return {
        "entity_id": "bulb-a",
        "selection_revision": revision,
        "source_id": "demo",
        "state": "on",
        "availability": "available",
        "received_at": received_at,
        "last_changed": "2020-01-01T00:00:00Z",
        "last_updated": "2020-01-01T00:00:00Z",
        "last_reported": None,
        **changes,
    }


def select(client, entity="bulb-a", selected=True):
    response = client.put(f"/api/selection/{entity}", json={"selected": selected}, headers=BROWSER)
    assert response.is_success, response.text
    return response.json()["revision"]


def baseline_list(client, entity="bulb-a"):
    return client.get("/api/baselines", params={"entity_id": entity}).json()["baselines"]


def test_baseline_is_attributed_retained_and_delayed_observations_cannot_bypass_selection(tmp_path):
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
        assert baseline["state"] == "on" and baseline["availability"] == "available"
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


def test_freshness_uses_confirmed_receipt_not_old_change_time_and_unchanged_snapshots_deduplicate(
    tmp_path,
):
    clock = [NOW]
    with TestClient(app_for(tmp_path / "freshness.sqlite", now=lambda: clock[0])) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        revision = select(client)
        assert client.post(
            "/internal/baselines", json=observation(revision), headers=AUTH
        ).is_success
        records = baseline_list(client)
        assert client.get("/api/monitored").json()["entities"][0]["freshness"] == "fresh"
        clock[0] = NOW + timedelta(seconds=121)
        assert client.get("/api/monitored").json()["entities"][0]["freshness"] == "stale"
        assert client.post(
            "/internal/baselines", json=observation(revision, clock[0].isoformat()), headers=AUTH
        ).is_success
        latest = client.get("/api/monitored").json()["entities"][0]
        assert latest["freshness"] == "fresh"
        assert baseline_list(client) == records
        assert client.post(
            "/internal/health",
            json={"source_id": "demo", "status": "failed", "received_at": clock[0].isoformat()},
            headers=AUTH,
        ).is_success
        assert client.get("/api/monitored").json()["entities"][0]["freshness"] == "stale"
        for invalid_receipt in ("2026-10-12T10:02:01", "2030-01-01T00:00:00Z"):
            assert client.post(
                "/internal/baselines", json=observation(revision, invalid_receipt), headers=AUTH
            ).is_client_error
        assert baseline_list(client) == records


def test_reselecting_requires_new_confirmation_before_showing_old_state(tmp_path):
    with TestClient(app_for(tmp_path / "reselect.sqlite")) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        revision = select(client)
        assert client.post(
            "/internal/baselines", json=observation(revision), headers=AUTH
        ).is_success
        records = baseline_list(client)
        select(client, selected=False)
        renewed = select(client)
        pending = client.get("/api/monitored").json()["entities"][0]
        assert pending["freshness"] == "pending"
        assert pending["baseline"] is None
        assert baseline_list(client) == records
        assert client.post(
            "/internal/baselines", json=observation(renewed), headers=AUTH
        ).is_success
        assert client.get("/api/monitored").json()["entities"][0]["freshness"] == "fresh"


def test_history_pages_are_bounded_and_retention_filters_before_cleanup(tmp_path):
    clock = [NOW]
    with TestClient(app_for(tmp_path / "pages.sqlite", now=lambda: clock[0])) as client:
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
        assert client.get("/api/monitored").json()["entities"][0]["baseline"]["brightness"] == 3
