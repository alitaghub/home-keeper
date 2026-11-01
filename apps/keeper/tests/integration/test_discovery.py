"""The catalog comes live from the reader; the selection is Home Keeper's own record."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient
from support.api import BROWSER, CATALOG_ENTITIES, NOW, TOKEN, app_for, catalog_reader
from support.reader import FakeReader

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


def lights(count: int) -> list[Entity]:
    return [
        Entity(
            id=f"bulb-{number:04}",
            device_id=f"device-{number:04}",
            device_name=f"Bulb {number}",
            name=f"Light {number}",
            device_kind="light",
            supported=True,
        )
        for number in range(count)
    ]


def test_a_501st_device_cannot_be_selected(database: str) -> None:
    reader = FakeReader(lights(501))
    with TestClient(app_for(database, reader=reader)) as client:
        for number in range(500):
            selected = client.put(
                f"/api/selection/bulb-{number:04}", json={"selected": True}, headers=BROWSER
            )
            assert selected.status_code == 200
        refused = client.put("/api/selection/bulb-0500", json={"selected": True}, headers=BROWSER)

        assert refused.status_code == 422, refused.text
        assert refused.json() == {"detail": "Selection is full"}
        selection = client.get("/api/selection").json()["entity_ids"]
        assert len(selection) == 500
        assert "bulb-0500" not in selection


def test_two_overlapping_selections_at_499_devices_store_exactly_500(database: str) -> None:
    reader = FakeReader(lights(502))
    with TestClient(app_for(database, reader=reader)) as client:
        for number in range(499):
            selected = client.put(
                f"/api/selection/bulb-{number:04}", json={"selected": True}, headers=BROWSER
            )
            assert selected.status_code == 200

        def select(entity_id: str, start: Barrier) -> int:
            start.wait()
            return client.put(
                f"/api/selection/{entity_id}", json={"selected": True}, headers=BROWSER
            ).status_code

        # Repeat the race: each round starts again from 499 devices.
        for _ in range(10):
            start = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(select, "bulb-0499", start)
                second = pool.submit(select, "bulb-0500", start)
                statuses = sorted([first.result(), second.result()])

            selection = client.get("/api/selection").json()["entity_ids"]
            assert statuses == [200, 422]
            assert len(selection) == 500
            for entity_id in ("bulb-0499", "bulb-0500"):
                if entity_id in selection:
                    client.put(
                        f"/api/selection/{entity_id}", json={"selected": False}, headers=BROWSER
                    ).raise_for_status()
            assert len(client.get("/api/selection").json()["entity_ids"]) == 499
