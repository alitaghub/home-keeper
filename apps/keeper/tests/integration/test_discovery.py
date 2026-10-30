"""The catalog comes live from the reader; the selection is Home Keeper's own record."""

from fastapi.testclient import TestClient
from support.api import BROWSER, CATALOG_ENTITIES, NOW, TOKEN, app_for, catalog_reader

from home_keeper.api import create_app
from home_keeper.models import Entity


def test_catalog_is_the_readers_catalog_at_request_time(database: str) -> None:
    reader = catalog_reader()
    with TestClient(app_for(database, reader=reader)) as client:
        body = client.get("/api/catalog").json()
        assert set(body) == {"source_id", "home_assistant_url", "entities"}
        assert body["entities"] == [Entity.model_validate(e).model_dump() for e in CATALOG_ENTITIES]

        reader.drop("bulb-b")
        ids = [entry["id"] for entry in client.get("/api/catalog").json()["entities"]]
        assert ids == ["bulb-a", "room", "plug"]


def test_catalog_tells_the_dashboard_where_home_assistant_is(database: str) -> None:
    app = create_app(
        database,
        TOKEN,
        now=lambda: NOW,
        reader=catalog_reader(),
        source_id="home_assistant",
        home_assistant_url="http://127.0.0.1:8123",
    )
    with TestClient(app) as client:
        body = client.get("/api/catalog").json()
        assert (body["source_id"], body["home_assistant_url"]) == (
            "home_assistant",
            "http://127.0.0.1:8123",
        )


def test_only_a_supported_catalog_entry_can_be_selected_and_the_selection_survives_restart(
    database: str,
) -> None:
    db = database
    with TestClient(app_for(db)) as client:
        assert client.get("/api/selection").json() == {"entity_ids": []}
        assert client.get("/api/monitored").json() == {"entities": []}
        for entity, status in (("room", 422), ("plug", 422), ("unknown", 404)):
            refused = client.put(
                f"/api/selection/{entity}", json={"selected": True}, headers=BROWSER
            )
            assert refused.status_code == status, refused.text
        assert client.get("/api/selection").json() == {"entity_ids": []}

        selected = client.put("/api/selection/bulb-a", json={"selected": True}, headers=BROWSER)
        assert selected.status_code == 200, selected.text
        assert selected.json() == {"entity_ids": ["bulb-a"]}
        again = client.put("/api/selection/bulb-a", json={"selected": True}, headers=BROWSER)
        assert again.json() == selected.json()

    with TestClient(app_for(db)) as restarted:
        assert restarted.get("/api/selection").json() == {"entity_ids": ["bulb-a"]}
        [monitored] = restarted.get("/api/monitored").json()["entities"]
        assert monitored["id"] == "bulb-a"


def test_a_rename_in_home_assistant_keeps_the_selection_and_shows_the_new_name(
    database: str,
) -> None:
    reader = catalog_reader()
    with TestClient(app_for(database, reader=reader)) as client:
        assert client.put(
            "/api/selection/bulb-b", json={"selected": True}, headers=BROWSER
        ).is_success
        reader.entities = [
            entity.model_copy(update={"name": "Renamed lamp"}) if entity.id == "bulb-b" else entity
            for entity in reader.entities
        ]
        assert client.get("/api/selection").json() == {"entity_ids": ["bulb-b"]}
        [monitored] = client.get("/api/monitored").json()["entities"]
        assert monitored["name"] == "Renamed lamp"
