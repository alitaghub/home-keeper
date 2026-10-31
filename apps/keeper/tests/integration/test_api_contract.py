"""Public API contract: response bodies stay stable and every route publishes a schema.

Snapshots in snapshots/api/ hold the parsed JSON each public route returns for a seeded state.
"""

import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from support.api import (
    BROWSER,
    NOW,
    TOKEN,
    Payload,
    catalog_reader,
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
from support.reader import FakeReader, history_row

from home_keeper.api import create_app
from home_keeper.models import Entity

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


def usage_range(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


def empty_responses(database: str) -> dict[str, Recorded]:
    app = create_app(database, TOKEN, now=lambda: NOW, reader=FakeReader())
    with TestClient(app) as client:
        return {
            "empty_catalog": record(client.get("/api/catalog")),
            "empty_selection": record(client.get("/api/selection")),
            "empty_events": record(client.get("/api/events")),
            "empty_health": record(client.get("/api/health")),
            "empty_monitored": record(client.get("/api/monitored")),
            "empty_usage": record(
                client.get("/api/usage", params=usage_range(NOW - timedelta(hours=1), NOW))
            ),
        }


def seeded_responses(database: str) -> dict[str, Recorded]:
    clock = [NOW]
    result: dict[str, Recorded] = {}
    reader = catalog_reader()
    app = create_app(database, TOKEN, now=lambda: clock[0], reader=reader)
    with TestClient(app) as client:
        result["put_selection"] = record(
            client.put("/api/selection/bulb-a", json={"selected": True}, headers=BROWSER)
        )
        selected = client.put("/api/selection/bulb-b", json={"selected": True}, headers=BROWSER)
        assert selected.is_success, selected.text
        # Home Assistant's recorder holds bulb-a unavailable, then off, then the three changes
        # below. Its history starts 10 s after the usage window does.
        reader.record(
            "bulb-a", history_row(DESK, NOW - timedelta(seconds=50), {"state": "unavailable"})
        )
        reader.record("bulb-a", history_row(DESK, NOW - timedelta(seconds=40), {"state": "off"}))
        for index, new in enumerate(["on", "off", "on"], start=1):
            at = NOW - timedelta(seconds=40 - 10 * index)
            state: Payload = (
                {"state": new, "brightness": 50 * index} if new == "on" else {"state": new}
            )
            reader.record("bulb-a", history_row(DESK, at, state))

        # bulb-b stays selected but disappears from Home Assistant.
        clock[0] = NOW + timedelta(seconds=7)
        reader.drop("bulb-b")
        # Home Assistant reports bulb-a off now.
        reader.set("bulb-a", DESK, {"state": "off"})

        clock[0] = NOW + timedelta(seconds=10)
        result["catalog"] = record(client.get("/api/catalog"))
        result["selection"] = record(client.get("/api/selection"))
        first = client.get("/api/events?limit=2")
        result["events_page_1"] = record(first)
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        result["events_page_2"] = record(
            client.get("/api/events", params={"limit": 2, "cursor": cursor})
        )
        result["health"] = record(client.get("/api/health"))
        result["monitored"] = record(client.get("/api/monitored"))
        # bulb-a: no history, then unavailable, then the changes. bulb-b, gone from Home
        # Assistant, has no history.
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


def unreachable_responses(database: str) -> dict[str, Recorded]:
    """What the dashboard and the agent get while Home Assistant cannot be reached."""
    reader = catalog_reader()
    app = create_app(database, TOKEN, now=lambda: NOW, reader=reader)
    with TestClient(app) as client:
        for entity in ("bulb-a", "bulb-b"):
            selected = client.put(
                f"/api/selection/{entity}", json={"selected": True}, headers=BROWSER
            )
            assert selected.is_success, selected.text
        reader.error = "connection"
        return {
            "health_unreachable": record(client.get("/api/health")),
            "catalog_unreachable": record(client.get("/api/catalog")),
            "monitored_unreachable": record(client.get("/api/monitored")),
            "error_select_unreachable": record(
                client.put("/api/selection/room", json={"selected": True}, headers=BROWSER)
            ),
            "events_unreachable": record(client.get("/api/events")),
            "usage_unreachable": record(
                client.get("/api/usage", params=usage_range(NOW - timedelta(hours=1), NOW))
            ),
            "deselect_unreachable": record(
                client.put("/api/selection/bulb-b", json={"selected": False}, headers=BROWSER)
            ),
        }


def control_responses(database: str) -> dict[str, Recorded]:
    """Control status, each command outcome, the command audit and the refusals."""
    clock = [NOW]
    controller = FakeController(clock)
    reader = catalog_reader()
    app = create_app(
        database,
        TOKEN,
        now=lambda: clock[0],
        reader=reader,
        controller=controller,
        command_wait_seconds=2,
        control_stale_seconds=15,
    )
    result: dict[str, Recorded] = {}
    with TestClient(app) as client:
        selected = client.put("/api/selection/bulb-a", json={"selected": True}, headers=BROWSER)
        assert selected.is_success, selected.text
        result["control_off"] = record(client.get("/api/control"))
        result["error_control_off"] = record(command(client, ON))
        beat(client, clock[0])
        result["control_on"] = record(client.get("/api/control"))
        result["error_command_unknown_entity"] = record(command(client, ON, entity="missing"))
        result["error_command_unsupported_entity"] = record(command(client, ON, entity="room"))
        result["error_not_monitored"] = record(command(client, ON, entity="bulb-b"))
        # Home Assistant has no state for bulb-a.
        result["error_state_unknown"] = record(command(client, ON))
        reader.set("bulb-a", DESK, {"state": "unavailable"})
        result["error_unavailable"] = record(command(client, ON))
        clock[0] += timedelta(seconds=1)
        reader.set("bulb-a", DESK, {"state": "off"})
        result["error_already_in_state"] = record(command(client, OFF))
        result["error_invalid_command"] = record(command(client, {"action": "toggle"}))

        # The light follows the command while the controller sends it.
        def follow() -> None:
            reader.set("bulb-a", DESK, {"state": "on", "brightness": 77})

        controller.on_send = follow
        result["command_observed"] = record(command(client, on(30), TELEGRAM))
        assert result["command_observed"]["body"]["outcome"] == "observed"
        controller.on_send = None
        controller.reply = "refused"
        result["command_refused"] = record(command(client, OFF, BROWSER_CHAT))
        controller.reply = "ok"

        # The light stays on. Each read moves the clock a second, so the wait ends.
        def tick() -> None:
            clock[0] += timedelta(seconds=1)

        reader.on_read = tick
        result["command_not_observed"] = record(command(client, OFF))
        assert result["command_not_observed"]["body"]["outcome"] == "not_observed"
        reader.on_read = None
        beat(client, clock[0])
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


THERMOSTAT: Payload = {
    "id": "thermostat",
    "device_id": "device-t",
    "device_name": "Hall thermostat",
    "name": "Hall heating",
    "device_kind": "state",
    "supported": True,
    "reason": None,
}
# A text sensor whose state and label are markup: the API returns both as plain strings.
NOTE: Payload = THERMOSTAT | {"id": "note", "device_id": "device-n", "name": "Note"}
PERSON: Payload = {
    "id": "ali",
    "device_id": None,
    "device_name": None,
    "name": "Ali",
    "supported": False,
    "reason": "Presence is not recorded",
}
MARKUP = "<img src=x onerror=alert(1)>"


def heating(state: str) -> Payload:
    return {"device_kind": "state", "state": state, "label": state.capitalize()}


def state_device_responses(database: str) -> dict[str, Recorded]:
    """A generic state device: its catalog entry, reading, changes, absence from usage and
    the refusal of a command."""
    clock = [NOW]
    controller = FakeController(clock)
    reader = FakeReader([Entity.model_validate(entry) for entry in (THERMOSTAT, NOTE, PERSON)])
    app = create_app(
        database,
        TOKEN,
        now=lambda: clock[0],
        reader=reader,
        controller=controller,
        command_wait_seconds=2,
        control_stale_seconds=15,
    )
    result: dict[str, Recorded] = {}
    with TestClient(app) as client:
        for entity in ("thermostat", "note"):
            selected = client.put(
                f"/api/selection/{entity}", json={"selected": True}, headers=BROWSER
            )
            assert selected.is_success, selected.text
        for seconds, state in ((50, "off"), (30, "heat"), (10, "off")):
            reader.record(
                "thermostat",
                history_row("climate.hall", NOW - timedelta(seconds=seconds), heating(state)),
            )
        reader.set("thermostat", "climate.hall", heating("off"))
        reader.set(
            "note", "sensor.note", {"device_kind": "state", "state": MARKUP, "label": MARKUP}
        )

        clock[0] = NOW + timedelta(seconds=10)
        result["state_catalog"] = record(client.get("/api/catalog"))
        result["state_monitored"] = record(client.get("/api/monitored"))
        result["state_events"] = record(client.get("/api/events"))
        usage = client.get("/api/usage", params=usage_range(NOW - timedelta(minutes=1), clock[0]))
        result["state_usage"] = record(usage)
        # Usage covers lights, plugs and switches only.
        assert usage.json()["devices"] == []

        beat(client, clock[0])
        result["error_state_command"] = record(command(client, ON, entity="thermostat"))
        assert result["error_state_command"]["status"] == 409
        assert result["error_state_command"]["body"]["detail"]["code"] == "not_supported"
        assert controller.sent == []
    return result


# pytest needs the case names before the responses exist, so the snapshot files name them.
SNAPSHOT_NAMES = sorted(path.stem for path in SNAPSHOTS.glob("*.json"))


@pytest.fixture(scope="module")
def responses(make_database: Callable[[], str]) -> dict[str, Recorded]:
    return (
        empty_responses(make_database())
        | seeded_responses(make_database())
        | unreachable_responses(make_database())
        | control_responses(make_database())
        | state_device_responses(make_database())
    )


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


def schema_app(database: str) -> FastAPI:
    return create_app(database, TOKEN, now=lambda: NOW, reader=FakeReader())


def test_every_public_route_publishes_a_response_schema(database: str) -> None:
    app = schema_app(database)
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


def test_internal_routes_are_not_published(database: str) -> None:
    paths = schema_app(database).openapi()["paths"]
    internal = [path for path in paths if path.startswith("/internal")]
    assert internal == [], f"internal routes in OpenAPI: {internal}"


def test_command_route_declares_its_refusal_body(database: str) -> None:
    operation = schema_app(database).openapi()["paths"]["/api/devices/{entity_id}/command"]["post"]
    schema = operation["responses"]["409"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/CommandRefused"}
