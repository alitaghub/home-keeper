from pathlib import Path

from fastapi.testclient import TestClient
from support.api import AUTH, BROWSER, CATALOG, CATALOG_ENTITIES, app_for


def test_discovery_requires_explicit_selection_and_selection_survives_restart(
    tmp_path: Path,
) -> None:
    db = tmp_path / "home.sqlite"
    with TestClient(app_for(db)) as client:
        response = client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        assert response.is_success, response.text
        catalog = client.get("/api/catalog").json()["entities"]
        assert {entry["id"] for entry in catalog} == {"bulb-a", "bulb-b", "room", "plug"}
        room = next(entry for entry in catalog if entry["id"] == "room")
        assert room["supported"] is False
        assert room["reason"]
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


def test_refresh_does_not_silently_revoke_previously_approved_missing_entity(
    tmp_path: Path,
) -> None:
    with TestClient(app_for(tmp_path / "refresh.sqlite")) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        selected = client.put(
            "/api/selection/bulb-a", json={"selected": True}, headers=BROWSER
        ).json()
        missing = {
            "source_id": "demo",
            "entities": [entry for entry in CATALOG_ENTITIES if entry["id"] != "bulb-a"],
        }
        assert client.post("/internal/catalog", json=missing, headers=AUTH).is_success
        assert client.get("/api/selection").json() == selected
        monitored = client.get("/api/monitored").json()["entities"]
        assert [entry["id"] for entry in monitored] == ["bulb-a"]
        assert monitored[0]["freshness"] in ("pending", "stale")
        removed = client.put("/api/selection/bulb-a", json={"selected": False}, headers=BROWSER)
        assert removed.is_success, removed.text
        assert removed.json()["entity_ids"] == []
