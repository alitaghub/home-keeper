"""Seed data and request builders for tests that drive the FastAPI app."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_keeper.api import create_app
from home_keeper.models import Entity
from home_keeper.store import Store
from support.reader import FakeReader

type Payload = dict[str, object]

TOKEN = "test-service-credential-sentinel"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BROWSER = {"X-Home-Keeper": "1"}
NOW = datetime(2026, 10, 12, 10, tzinfo=UTC)
CATALOG_ENTITIES: list[Payload] = [
    {
        "id": "bulb-a",
        "device_id": "device-a",
        "device_name": "Hue bulb",
        "name": "<script>alert('device')</script>",
        "device_kind": "light",
        "supported": True,
        "reason": None,
    },
    {
        "id": "bulb-b",
        "device_id": "device-b",
        "device_name": "Second bulb",
        "name": "Second light",
        "device_kind": "light",
        "supported": True,
        "reason": None,
    },
    {
        "id": "room",
        "device_id": None,
        "device_name": None,
        "name": "Living room",
        "supported": False,
        "reason": "Groups and rooms are not recorded",
    },
    {
        "id": "plug",
        "device_id": "device-plug",
        "device_name": "Plug",
        "name": "Plug state",
        "supported": False,
        "reason": "Only lights, plugs and switches are supported",
    },
]
CATALOG: Payload = {"source_id": "demo", "entities": CATALOG_ENTITIES}


def catalog_reader() -> FakeReader:
    """A reader whose catalog is `CATALOG_ENTITIES` and that reports no device states."""
    return FakeReader([Entity.model_validate(entry) for entry in CATALOG_ENTITIES])


def app_for(
    path: Path, now: Callable[[], datetime] = lambda: NOW, reader: FakeReader | None = None
) -> FastAPI:
    return create_app(db_path=path, service_token=TOKEN, now=now, reader=reader or catalog_reader())


def observation(revision: int, received_at: str = NOW.isoformat(), **changes: object) -> Payload:
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


def event(revision: int, identity: str = "change-1", **changes: object) -> Payload:
    return {
        "id": identity,
        "entity_id": "bulb-a",
        "source_id": "demo",
        "selection_revision": revision,
        "event_time": NOW.isoformat(),
        "received_at": NOW.isoformat(),
        "old": {"state": "off"},
        "new": {"state": "on", "brightness": 100},
    } | changes


def select(client: TestClient, entity: str = "bulb-a", *, selected: bool = True) -> int:
    """Change the selection and return the revision the collector's ingest routes check.

    The public selection no longer carries a revision; the collector's own route does, for
    as long as the ingest routes exist.
    """
    response = client.put(f"/api/selection/{entity}", json={"selected": selected}, headers=BROWSER)
    assert response.is_success, response.text
    revision: int = client.get("/internal/selection", headers=AUTH).json()["revision"]
    return revision


def baseline_list(client: TestClient, entity: str = "bulb-a") -> list[dict[str, Any]]:
    baselines: list[dict[str, Any]] = client.get(
        "/api/baselines", params={"entity_id": entity}
    ).json()["baselines"]
    return baselines


def recorded_current(path: Path, entity_id: str = "bulb-a") -> dict[str, Any]:
    """The recorder's newest reading of a device, which /api/monitored showed before it read
    Home Assistant live. The recorder keeps it until the copied tables are removed."""
    with Store(path).transaction("DEFERRED") as db:
        current = Store.current(db, entity_id)
    assert current is not None, f"nothing recorded for {entity_id}"
    reading: dict[str, Any] = current.model_dump(mode="json", exclude_unset=True)
    return reading
