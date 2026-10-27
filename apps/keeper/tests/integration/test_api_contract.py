"""Public API contract: response bodies stay stable and every route publishes a schema.

Snapshots in snapshots/api/ hold the parsed JSON each public route returns for a seeded state.
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
from support.control import (
    BROWSER_CHAT,
    DESK,
    OFF,
    ON,
    TELEGRAM,
    FakeController,
    beat,
    command,
    on,
)

from home_keeper.api import create_app

SNAPSHOTS = Path(__file__).parent / "snapshots" / "api"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# A recorded response: its status and normalized JSON body.
type Recorded = dict[str, Any]


def normalize(body: object) -> Any:
    """Replace random uuid4 IDs with placeholders numbered in key-sorted JSON order.

    Sorting makes numbering ignore key order while cross-field ID references still must match.
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


def usage_range(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


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
            "empty_usage": record(
                client.get("/api/usage", params=usage_range(NOW - timedelta(hours=1), NOW))
            ),
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
            at = NOW - timedelta(seconds=40 - 10 * index)
            payload = event(
                revision,
                identity=f"change-{index}",
                event_time=at.isoformat(),
                received_at=(at + timedelta(seconds=3)).isoformat(),
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
        # Covers the events, the recovered whole-home gap and the open bulb-a gap above.
        usage = usage_range(NOW - timedelta(minutes=1), clock[0])
        result["usage"] = record(client.get("/api/usage", params=usage))

        # Errors the dashboard handles.
        result["error_unknown_entity"] = record(
            client.put("/api/selection/missing", json={"selected": True}, headers=BROWSER)
        )
        result["error_unsupported_entity"] = record(
            client.put("/api/selection/room", json={"selected": True}, headers=BROWSER)
        )
        result["error_invalid_cursor"] = record(client.get("/api/events?cursor=not-a-cursor"))
        result["error_usage_reversed_range"] = record(
            client.get("/api/usage", params=usage_range(clock[0], NOW))
        )
    return result


def control_responses(tmp_path: Path) -> dict[str, Recorded]:
    """Control status, each command outcome, the command audit and the refusals."""
    clock = [NOW]
    controller = FakeController(clock)
    app = create_app(
        tmp_path / "control.sqlite",
        TOKEN,
        now=lambda: clock[0],
        controller=controller,
        command_wait_seconds=0.2,
        control_stale_seconds=15,
    )
    result: dict[str, Recorded] = {}
    with TestClient(app) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        result["control_off"] = record(client.get("/api/control"))
        result["error_control_off"] = record(command(client, ON))
        beat(client, clock[0])
        result["control_on"] = record(client.get("/api/control"))
        result["error_command_unknown_entity"] = record(command(client, ON, entity="missing"))
        result["error_command_unsupported_entity"] = record(command(client, ON, entity="room"))
        result["error_not_monitored"] = record(command(client, ON, entity="bulb-b"))
        result["error_state_unknown"] = record(command(client, ON))
        unavailable = observation(
            revision, state="unavailable", availability="unavailable", source_entity_id=DESK
        )
        client.post("/internal/baselines", json=unavailable, headers=AUTH)
        result["error_unavailable"] = record(command(client, ON))
        clock[0] += timedelta(seconds=1)
        off = observation(revision, clock[0].isoformat(), state="off", source_entity_id=DESK)
        client.post("/internal/baselines", json=off, headers=AUTH)
        result["error_already_in_state"] = record(command(client, OFF))
        result["error_invalid_command"] = record(command(client, {"action": "toggle"}))

        def report(at: datetime) -> None:
            payload = event(
                revision,
                "after-send",
                source_entity_id=DESK,
                event_time=at.isoformat(),
                received_at=at.isoformat(),
                old={"state": "off"},
                new={"state": "on", "brightness": 77},
            )
            client.post("/internal/events", json=payload, headers=AUTH).raise_for_status()

        controller.deliver = report
        result["command_observed"] = record(command(client, on(30), TELEGRAM))
        controller.join()
        controller.deliver = None
        controller.reply = "refused"
        result["command_refused"] = record(command(client, OFF, BROWSER_CHAT))
        controller.reply = "ok"
        result["command_not_observed"] = record(command(client, OFF))
        controller.unreachable = True
        result["command_unreachable"] = record(command(client, OFF))

        result["commands"] = record(client.get("/api/commands"))
        first = client.get("/api/commands", params={"limit": 2})
        result["commands_page_1"] = record(first)
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        result["commands_page_2"] = record(
            client.get("/api/commands", params={"limit": 2, "cursor": cursor})
        )
        result["error_commands_invalid_cursor"] = record(
            client.get("/api/commands", params={"cursor": "not-a-cursor"})
        )
    return result


# pytest needs the case names before the responses exist, so the snapshot files name them.
SNAPSHOT_NAMES = sorted(path.stem for path in SNAPSHOTS.glob("*.json"))


@pytest.fixture(scope="module")
def responses(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Recorded]:
    path = tmp_path_factory.mktemp("contract")
    return empty_responses(path) | seeded_responses(path) | control_responses(path)


def snapshot_text(recorded: Recorded) -> str:
    return json.dumps(recorded, indent=2, ensure_ascii=False) + "\n"


def test_every_recorded_response_has_a_snapshot(responses: dict[str, Recorded]) -> None:
    if UPDATE:
        for path in SNAPSHOTS.glob("*.json"):
            path.unlink()
        for name, recorded in responses.items():
            (SNAPSHOTS / f"{name}.json").write_text(snapshot_text(recorded))
    assert sorted(responses) == SNAPSHOT_NAMES


@pytest.mark.parametrize("name", SNAPSHOT_NAMES)
def test_public_response_matches_snapshot(responses: dict[str, Recorded], name: str) -> None:
    assert responses[name] == json.loads((SNAPSHOTS / f"{name}.json").read_text())


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


def test_command_route_declares_its_refusal_body(tmp_path: Path) -> None:
    operation = schema_app(tmp_path).openapi()["paths"]["/api/lights/{entity_id}/command"]["post"]
    schema = operation["responses"]["409"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/CommandRefused"}
