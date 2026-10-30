"""Seed data and request builders for tests that drive the FastAPI app."""

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI

from home_keeper.api import create_app
from home_keeper.models import Entity
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


def catalog_reader() -> FakeReader:
    """A reader whose catalog is `CATALOG_ENTITIES` and that reports no device states."""
    return FakeReader([Entity.model_validate(entry) for entry in CATALOG_ENTITIES])


def app_for(
    database: str, now: Callable[[], datetime] = lambda: NOW, reader: FakeReader | None = None
) -> FastAPI:
    return create_app(database, service_token=TOKEN, now=now, reader=reader or catalog_reader())
