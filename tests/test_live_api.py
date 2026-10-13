from fastapi.testclient import TestClient
from test_discovery import AUTH, BROWSER, NOW, TOKEN

from home_observer.api import create_app


def test_live_catalog_is_source_bound_and_refresh_requires_browser_boundary(tmp_path):
    with TestClient(
        create_app(tmp_path / "live.sqlite", TOKEN, now=lambda: NOW, source_id="home_assistant")
    ) as client:
        payload = {
            "source_id": "home_assistant",
            "entities": [{"id": "ha:registry1", "name": "Hue bulb", "supported": True}],
        }
        assert client.post("/internal/catalog", json=payload, headers=AUTH).status_code == 200
        assert client.get("/api/catalog").json()["source_id"] == "home_assistant"
        assert (
            client.post(
                "/internal/catalog", json=payload | {"source_id": "demo"}, headers=AUTH
            ).status_code
            == 409
        )
        assert client.post("/api/discovery/refresh").status_code == 403
        assert client.post("/api/discovery/refresh", headers=BROWSER).status_code == 200
        assert client.get("/internal/selection", headers=AUTH).json()["refresh_revision"] == 1
        assert (
            client.put(
                "/api/selection/ha:registry1", json={"selected": True}, headers=BROWSER
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/internal/health",
                headers=AUTH,
                json={
                    "source_id": "home_assistant",
                    "status": "failed",
                    "received_at": NOW.isoformat(),
                    "error": "authentication",
                },
            ).status_code
            == 200
        )
        assert client.get("/api/catalog").json()["freshness"] == "stale"
        assert client.get("/api/monitored").json()["entities"][0]["baseline"] is None
        assert client.get("/api/selection").json()["entity_ids"] == ["ha:registry1"]


def test_database_cannot_switch_source_or_accept_foreign_observations(tmp_path):
    import pytest

    path = tmp_path / "home.sqlite"
    with TestClient(create_app(path, TOKEN, now=lambda: NOW, source_id="home_assistant")) as client:
        assert (
            client.post(
                "/internal/health",
                headers=AUTH,
                json={"source_id": "demo", "status": "healthy", "received_at": NOW.isoformat()},
            ).status_code
            == 409
        )
        assert client.get("/api/health").json()["status"] == "pending"
    with pytest.raises(ValueError, match="separate database"):
        create_app(path, TOKEN)


def test_registry_identity_preserves_selection_on_rename_without_granting_replacement(tmp_path):
    with TestClient(
        create_app(tmp_path / "rename.sqlite", TOKEN, now=lambda: NOW, source_id="home_assistant")
    ) as client:
        entry = {"id": "ha:original", "name": "Old lamp", "supported": True}
        assert client.post(
            "/internal/catalog",
            headers=AUTH,
            json={"source_id": "home_assistant", "entities": [entry]},
        ).is_success
        response = client.put(
            "/api/selection/ha:original", headers=BROWSER, json={"selected": True}
        )
        revision = response.json()["revision"]
        observation = {
            "source_id": "home_assistant",
            "entity_id": "ha:original",
            "selection_revision": revision,
            "state": "off",
            "availability": "available",
            "received_at": NOW.isoformat(),
        }
        assert client.post("/internal/baselines", headers=AUTH, json=observation).is_success
        records = client.get("/api/baselines?entity_id=ha:original").json()
        assert client.post(
            "/internal/catalog",
            headers=AUTH,
            json={
                "source_id": "home_assistant",
                "entities": [entry | {"name": "Renamed lamp"}, entry | {"id": "ha:replacement"}],
            },
        ).is_success
        assert client.get("/api/selection").json()["entity_ids"] == ["ha:original"]
        assert client.get("/api/baselines?entity_id=ha:original").json() == records
        assert (
            client.post(
                "/internal/baselines",
                headers=AUTH,
                json=observation | {"entity_id": "ha:replacement"},
            ).status_code
            == 409
        )
