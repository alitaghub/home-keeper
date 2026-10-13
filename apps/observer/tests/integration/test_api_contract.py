"""Public API contract: response bodies stay stable and every route publishes a schema.

Snapshots in tests/integration/snapshots/api/ hold the exact parsed JSON each public route
returned for a seeded state. Regenerate them only when a response change is intended:

    UPDATE_SNAPSHOTS=1 uv run pytest apps/observer/tests/integration/test_api_contract.py
"""

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from support.api import (
    AUTH,
    BROWSER,
    CATALOG,
    CATALOG_ENTITIES,
    NOW,
    TOKEN,
    Payload,
    event,
    observation,
    select,
)

from home_observer.api import create_app

SNAPSHOTS = Path(__file__).parent / "snapshots" / "api"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# A recorded response: its status and normalized JSON body.
type Recorded = dict[str, Any]


def normalize(body: object) -> Any:
    """Replace random uuid4 identifiers (baseline and gap IDs) with stable placeholders.

    Placeholders are numbered by first appearance in key-sorted JSON, so references
    between fields (a recovery baseline's coverage_gap_ids pointing at a gap) still
    have to match, while key order, which parsed-JSON equality ignores, does not.
    """
    seen: dict[str, str] = {}
    text = UUID.sub(
        lambda match: seen.setdefault(match.group(), f"<uuid-{len(seen) + 1}>"),
        json.dumps(body, sort_keys=True),
    )
    return json.loads(text)


def record(response: httpx.Response) -> Recorded:
    return {"status": response.status_code, "body": normalize(response.json())}


def health(clock: datetime) -> Payload:
    return {"source_id": "demo", "status": "healthy", "received_at": clock.isoformat()}


def coverage(clock: datetime, status: str = "interrupted", **changes: object) -> Payload:
    return {"source_id": "demo", "status": status, "received_at": clock.isoformat()} | changes


def empty_responses(tmp_path: Path) -> dict[str, Recorded]:
    with TestClient(create_app(tmp_path / "empty.sqlite", TOKEN, now=lambda: NOW)) as client:
        return {
            "empty_catalog": record(client.get("/api/catalog")),
            "empty_selection": record(client.get("/api/selection")),
            "empty_baselines": record(client.get("/api/baselines?entity_id=bulb-a")),
            "empty_events": record(client.get("/api/events")),
            "empty_coverage": record(client.get("/api/coverage")),
            "empty_health": record(client.get("/api/health")),
            "empty_monitored": record(client.get("/api/monitored")),
        }


def seeded_responses(tmp_path: Path) -> dict[str, Recorded]:
    clock = [NOW]
    result: dict[str, Recorded] = {}
    app = create_app(tmp_path / "seeded.sqlite", TOKEN, now=lambda: clock[0])
    with TestClient(app) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        result["put_selection"] = record(
            client.put("/api/selection/bulb-a", json={"selected": True}, headers=BROWSER)
        )
        revision = select(client, "bulb-b")
        client.post("/internal/health", json=health(clock[0]), headers=AUTH)
        client.post("/internal/baselines", json=observation(revision), headers=AUTH)
        for index, (old, new) in enumerate([("off", "on"), ("on", "off"), ("off", "on")], start=1):
            at = (NOW - timedelta(seconds=40 - 10 * index)).isoformat()
            payload = event(
                revision,
                identity=f"change-{index}",
                event_time=at,
                received_at=at,
                old={"state": old},
                new={"state": new, "brightness": 50 * index} if new == "on" else {"state": new},
            )
            assert client.post("/internal/events", json=payload, headers=AUTH).is_success

        # A whole-home gap that recovers with a snapshot.
        clock[0] = NOW + timedelta(seconds=5)
        client.post("/internal/coverage", json=coverage(clock[0]), headers=AUTH)
        clock[0] = NOW + timedelta(seconds=6)
        snapshot = observation(revision, received_at=clock[0].isoformat(), state="off")
        assert client.post("/internal/baselines", json=snapshot, headers=AUTH).is_success
        recovered = coverage(clock[0], "recovered", selection_revision=revision)
        assert client.post("/internal/coverage", json=recovered, headers=AUTH).is_success

        # bulb-b stays selected but disappears from discovery.
        clock[0] = NOW + timedelta(seconds=7)
        catalog = CATALOG | {"entities": [e for e in CATALOG_ENTITIES if e["id"] != "bulb-b"]}
        client.post("/internal/catalog", json=catalog, headers=AUTH)

        # An entity-scoped gap that stays open.
        clock[0] = NOW + timedelta(seconds=8)
        open_gap = coverage(clock[0], reason="reconciliation", entity_id="bulb-a")
        assert client.post("/internal/coverage", json=open_gap, headers=AUTH).is_success
        client.post("/internal/health", json=health(clock[0]), headers=AUTH)
        result["discovery_refresh"] = record(client.post("/api/discovery/refresh", headers=BROWSER))

        clock[0] = NOW + timedelta(seconds=10)
        result["catalog"] = record(client.get("/api/catalog"))
        result["selection"] = record(client.get("/api/selection"))
        result["baselines"] = record(client.get("/api/baselines?entity_id=bulb-a"))
        first = client.get("/api/baselines?entity_id=bulb-a&limit=1")
        result["baselines_page_1"] = record(first)
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        result["baselines_page_2"] = record(
            client.get(f"/api/baselines?entity_id=bulb-a&limit=1&cursor={cursor}")
        )
        first = client.get("/api/events?limit=2")
        result["events_page_1"] = record(first)
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        result["events_page_2"] = record(
            client.get("/api/events", params={"limit": 2, "cursor": cursor})
        )
        result["coverage"] = record(client.get("/api/coverage"))
        result["health"] = record(client.get("/api/health"))
        result["monitored"] = record(client.get("/api/monitored"))

        # Errors the dashboard handles.
        result["error_unknown_entity"] = record(
            client.put("/api/selection/missing", json={"selected": True}, headers=BROWSER)
        )
        result["error_unsupported_entity"] = record(
            client.put("/api/selection/room", json={"selected": True}, headers=BROWSER)
        )
        result["error_invalid_cursor"] = record(client.get("/api/events?cursor=not-a-cursor"))
    return result


SNAPSHOT_NAMES = [
    "empty_catalog",
    "empty_selection",
    "empty_baselines",
    "empty_events",
    "empty_coverage",
    "empty_health",
    "empty_monitored",
    "put_selection",
    "discovery_refresh",
    "catalog",
    "selection",
    "baselines",
    "baselines_page_1",
    "baselines_page_2",
    "events_page_1",
    "events_page_2",
    "coverage",
    "health",
    "monitored",
    "error_unknown_entity",
    "error_unsupported_entity",
    "error_invalid_cursor",
]


@pytest.fixture(scope="module")
def responses(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Recorded]:
    path = tmp_path_factory.mktemp("contract")
    return empty_responses(path) | seeded_responses(path)


def test_snapshot_list_covers_every_recorded_response(responses: dict[str, Recorded]) -> None:
    assert sorted(responses) == sorted(SNAPSHOT_NAMES)


@pytest.mark.parametrize("name", SNAPSHOT_NAMES)
def test_public_response_matches_snapshot(responses: dict[str, Recorded], name: str) -> None:
    path = SNAPSHOTS / f"{name}.json"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(responses[name], indent=2, ensure_ascii=False) + "\n")
    assert responses[name] == json.loads(path.read_text())


def schema_app(tmp_path: Path) -> FastAPI:
    return create_app(tmp_path / "schema.sqlite", TOKEN, now=lambda: NOW)


def test_every_public_route_publishes_a_response_schema(tmp_path: Path) -> None:
    app = schema_app(tmp_path)
    paths = app.openapi()["paths"]
    public = [
        (route.path, method.lower())
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/")
        for method in route.methods or ()
        if method != "HEAD"
    ]
    assert public, "expected public /api routes"
    untyped: list[str] = []
    for path, method in public:
        operation: dict[str, Any] = paths.get(path, {}).get(method) or {}
        operation_responses: dict[str, Any] = operation.get("responses", {})
        schemas = [
            response.get("content", {}).get("application/json", {}).get("schema")
            for status, response in operation_responses.items()
            if status.startswith("2")
        ]
        if not any(schemas):
            untyped.append(f"{method.upper()} {path}")
    assert untyped == [], f"public routes without a response schema: {untyped}"


def test_internal_routes_are_not_published(tmp_path: Path) -> None:
    paths = schema_app(tmp_path).openapi()["paths"]
    internal = [path for path in paths if path.startswith("/internal")]
    assert internal == [], f"internal routes in OpenAPI: {internal}"
