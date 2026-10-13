from datetime import UTC, datetime

from fastapi.testclient import TestClient

from home_observer.api import create_app

TOKEN = "test-service-credential-sentinel"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BROWSER = {"X-Home-Observer": "1"}
NOW = datetime(2026, 10, 12, 10, tzinfo=UTC)
CATALOG = {
    "source_id": "demo",
    "entities": [
        {
            "id": "bulb-a",
            "device_id": "device-a",
            "device_name": "Hue bulb",
            "name": "<script>alert('device')</script>",
            "supported": True,
            "reason": None,
        },
        {
            "id": "bulb-b",
            "device_id": "device-b",
            "device_name": "Second bulb",
            "name": "Second light",
            "supported": True,
            "reason": None,
        },
        {
            "id": "room",
            "device_id": None,
            "device_name": None,
            "name": "Living room",
            "supported": False,
            "reason": "Room aggregate",
        },
        {
            "id": "plug",
            "device_id": "device-plug",
            "device_name": "Plug",
            "name": "Plug state",
            "supported": False,
            "reason": "Not an individual Hue light",
        },
    ],
}


def app_for(path, now=lambda: NOW):
    return create_app(db_path=path, service_token=TOKEN, now=now, reconciliation_seconds=60)


def test_discovery_requires_explicit_selection_and_selection_survives_restart(tmp_path):
    db = tmp_path / "home.sqlite"
    with TestClient(app_for(db)) as client:
        response = client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        assert response.is_success, response.text
        catalog = client.get("/api/catalog").json()["entities"]
        assert {entry["id"] for entry in catalog} == {"bulb-a", "bulb-b", "room", "plug"}
        room = next(entry for entry in catalog if entry["id"] == "room")
        assert room["supported"] is False and room["reason"]
        initial = client.get("/api/selection").json()
        assert initial["entity_ids"] == []
        assert client.get("/api/monitored").json()["entities"] == []
        for entity in ("room", "plug", "unknown"):
            assert client.put(
                f"/api/selection/{entity}", json={"selected": True}, headers=BROWSER
            ).is_client_error
            assert client.get("/api/selection").json() == initial
        selected = client.put("/api/selection/bulb-a", json={"selected": True}, headers=BROWSER)
        assert selected.is_success, selected.text
        assert selected.json()["entity_ids"] == ["bulb-a"]
        assert selected.json()["revision"] > initial["revision"]
        assert (
            client.put("/api/selection/bulb-a", json={"selected": True}, headers=BROWSER).json()
            == selected.json()
        )
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        assert client.get("/api/selection").json() == selected.json()
        assert (
            client.get("/api/baselines", params={"entity_id": "bulb-b"}).json()["baselines"] == []
        )
    with TestClient(app_for(db)) as restarted:
        assert restarted.get("/api/selection").json() == selected.json()
        monitored = restarted.get("/api/monitored").json()["entities"]
        assert len(monitored) == 1
        assert monitored[0]["id"] == "bulb-a"
        assert monitored[0]["baseline"] is None
        assert monitored[0]["freshness"] == "pending"


def test_refresh_does_not_silently_revoke_previously_approved_missing_entity(tmp_path):
    with TestClient(app_for(tmp_path / "refresh.sqlite")) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        selected = client.put(
            "/api/selection/bulb-a", json={"selected": True}, headers=BROWSER
        ).json()
        missing = {
            "source_id": "demo",
            "entities": [entry for entry in CATALOG["entities"] if entry["id"] != "bulb-a"],
        }
        assert client.post("/internal/catalog", json=missing, headers=AUTH).is_success
        assert client.get("/api/selection").json() == selected
        monitored = client.get("/api/monitored").json()["entities"]
        assert [entry["id"] for entry in monitored] == ["bulb-a"]
        assert monitored[0]["freshness"] in ("pending", "stale")
        removed = client.put("/api/selection/bulb-a", json={"selected": False}, headers=BROWSER)
        assert removed.is_success, removed.text
        assert removed.json()["entity_ids"] == []
