"""Seed data and request builders for tests that drive the FastAPI app."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_observer.api import create_app

type Payload = dict[str, object]

TOKEN = "test-service-credential-sentinel"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BROWSER = {"X-Home-Observer": "1"}
NOW = datetime(2026, 10, 12, 10, tzinfo=UTC)
CATALOG_ENTITIES: list[Payload] = [
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
]
CATALOG: Payload = {"source_id": "demo", "entities": CATALOG_ENTITIES}


def app_for(path: Path, now: Callable[[], datetime] = lambda: NOW) -> FastAPI:
    return create_app(db_path=path, service_token=TOKEN, now=now, reconciliation_seconds=60)


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
    response = client.put(f"/api/selection/{entity}", json={"selected": selected}, headers=BROWSER)
    assert response.is_success, response.text
    revision: int = response.json()["revision"]
    return revision


def baseline_list(client: TestClient, entity: str = "bulb-a") -> list[dict[str, Any]]:
    baselines: list[dict[str, Any]] = client.get(
        "/api/baselines", params={"entity_id": entity}
    ).json()["baselines"]
    return baselines
