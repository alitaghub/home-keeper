"""Readings stored before Home Keeper recorded plugs and switches still read as lights."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from support import previous_release
from support.api import (
    AUTH,
    CATALOG,
    CATALOG_ENTITIES,
    NOW,
    TOKEN,
    Payload,
    app_for,
    event,
    observation,
    select,
)
from support.control import OFF, ON, TELEGRAM, FakeController, beat, command

from home_keeper.api import create_app

# Rows exactly as the light-only Home Keeper stored them: no `device_kind` anywhere.
BASELINE = (
    '{"state":"on","availability":"available","last_changed":"2020-01-01T00:00:00Z",'
    '"last_updated":"2020-01-01T00:00:00Z","last_reported":null,"brightness":null,'
    '"color_mode":null,"color_temp_kelvin":null,"hs_color":null,"xy_color":null,'
    '"rgb_color":null,"rgbw_color":null,"rgbww_color":null,"id":"baseline-1",'
    '"kind":"baseline","entity_id":"bulb-a","source_entity_id":null,"source_id":"demo",'
    '"received_at":"2026-10-12T10:00:00Z","collection_started_at":null,'
    '"persisted_at":"2026-10-12T10:00:00+00:00"}'
)
EVENT = (
    '{"id": "change-1", "entity_id": "bulb-a", "source_entity_id": null, "source_id": "demo",'
    ' "selection_revision": 1, "event_time": "2026-10-12T10:00:00Z",'
    ' "received_at": "2026-10-12T10:00:00Z", "old": {"state": "off", "availability": "available"},'
    ' "new": {"brightness": 100, "state": "on", "availability": "available"},'
    ' "persisted_at": "2026-10-12T10:00:00+00:00"}'
)
CURRENT = (
    '{"state":"on","availability":"available","brightness":100,"id":"change-1","kind":"event",'
    '"entity_id":"bulb-a","source_entity_id":null,"source_id":"demo",'
    '"event_time":"2026-10-12T10:00:00+00:00","received_at":"2026-10-12T10:00:00+00:00",'
    '"persisted_at":"2026-10-12T10:00:00+00:00"}'
)
COMMAND = (
    '{"id": "command-1", "entity_id": "bulb-a", "command": {"action": "on", "brightness_pct": 40},'
    ' "sender": {"kind": "telegram", "id": 424242, "name": "Ali"},'
    ' "sent_at": "2026-10-12T10:00:00+00:00", "outcome": "observed", "event_id": "change-1"}'
)
STORED = "2026-10-12T10:00:00+00:00"


def test_light_history_stored_before_plugs_and_switches_reads_as_lights(tmp_path: Path) -> None:
    path = tmp_path / "home.sqlite"
    with TestClient(app_for(path)) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH).raise_for_status()
        assert select(client) == 1
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO baselines(id, entity_id, data, persisted_at) VALUES(?, ?, ?, ?)",
            ("baseline-1", "bulb-a", BASELINE, STORED),
        )
        db.execute(
            "INSERT INTO events VALUES(?, ?, ?, ?, ?, ?)",
            ("demo", "change-1", "bulb-a", STORED, STORED, EVENT),
        )
        db.execute(
            "INSERT INTO current_state(entity_id, source_time, data, snapshot_started_at,"
            " snapshot_received_at) VALUES(?, ?, ?, ?, ?)",
            ("bulb-a", STORED, CURRENT, STORED, STORED),
        )
        db.execute("INSERT INTO confirmed VALUES(?, ?, ?)", ("bulb-a", STORED, STORED))
        db.execute(
            "INSERT INTO commands(id, entity_id, sent_at, data) VALUES(?, ?, ?, ?)",
            ("command-1", "bulb-a", STORED, COMMAND),
        )

    with TestClient(app_for(path)) as client:
        [baseline] = client.get("/api/baselines", params={"entity_id": "bulb-a"}).json()[
            "baselines"
        ]
        assert (baseline["kind"], baseline.get("device_kind")) == ("baseline", "light")
        [event] = client.get("/api/events").json()["events"]
        assert event.get("device_kind") == "light"
        assert event["new"]["brightness"] == 100
        [monitored] = client.get("/api/monitored").json()["entities"]
        current = monitored["baseline"]
        assert (current["kind"], current.get("device_kind")) == ("event", "light")
        assert current["brightness"] == 100
        [sent] = client.get("/api/commands").json()["commands"]
        assert sent == {
            "id": "command-1",
            "entity_id": "bulb-a",
            "device_kind": "light",
            "command": {"action": "on", "brightness_pct": 40},
            "sender": {"kind": "telegram", "id": 424242, "name": "Ali"},
            "sent_at": STORED,
            "outcome": "observed",
            "event_id": "change-1",
        }
    # History is read as lights, not rewritten.
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT data FROM events").fetchone()[0] == EVENT
        assert db.execute("SELECT data FROM baselines").fetchone()[0] == BASELINE
        assert db.execute("SELECT data FROM commands").fetchone()[0] == COMMAND


PLUG: Payload = {
    "id": "plug-a",
    "device_id": "device-plug-a",
    "device_name": "Kettle plug",
    "name": "Kettle",
    "device_kind": "switch",
    "supported": True,
    "reason": None,
}
SWITCH: Payload = {"device_kind": "switch"}


def stored(path: Path, table: str) -> dict[str, dict[str, Any]]:
    """Each row's decoded `data` in `table`, by entity id."""
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(f"SELECT entity_id, data FROM {table}").fetchall()  # noqa: S608 -- fixed table names from the test
    return {entity: json.loads(data) for entity, data in rows}


def test_light_rows_written_now_are_readable_by_the_light_only_release(tmp_path: Path) -> None:
    """A downgrade must not fail on stored light rows; switch rows say what they are."""
    path = tmp_path / "home.sqlite"
    clock = [NOW]
    controller = FakeController(clock)
    app = create_app(
        path, TOKEN, now=lambda: clock[0], controller=controller, command_wait_seconds=0.1
    )
    catalog = {"source_id": "demo", "entities": [*CATALOG_ENTITIES, PLUG]}
    with TestClient(app) as client:
        client.post("/internal/catalog", json=catalog, headers=AUTH).raise_for_status()
        select(client, "bulb-a")
        revision = select(client, "plug-a")
        for payload in (
            observation(revision, source_entity_id="light.desk", brightness=40),
            observation(
                revision, entity_id="plug-a", source_entity_id="switch.kettle", device_kind="switch"
            ),
        ):
            client.post("/internal/baselines", json=payload, headers=AUTH).raise_for_status()
        for payload in (
            event(revision, "light-on", source_entity_id="light.desk"),
            event(
                revision,
                "plug-off",
                entity_id="plug-a",
                source_entity_id="switch.kettle",
                old={"state": "on"} | SWITCH,
                new={"state": "off"} | SWITCH,
            ),
        ):
            response = client.post("/internal/events", json=payload, headers=AUTH)
            assert response.json()["recorded"], response.text
        beat(client, NOW)
        for entity, action in (("bulb-a", OFF), ("plug-a", ON)):
            response = command(client, action, TELEGRAM, entity=entity)
            assert response.status_code == 200, response.text
        controller.join()

    rows = {
        "baselines": stored(path, "baselines"),
        "events": stored(path, "events"),
        "current_state": stored(path, "current_state"),
        "commands": stored(path, "commands"),
    }
    for table, by_entity in rows.items():
        light, switch = by_entity["bulb-a"], by_entity["plug-a"]
        assert "device_kind" not in light, (table, light)
        assert switch["device_kind"] == "switch", (table, switch)
    for state in ("old", "new"):
        assert "device_kind" not in rows["events"]["bulb-a"][state]
        assert rows["events"]["plug-a"][state]["device_kind"] == "switch"

    previous_release.BaselineRecord.model_validate(rows["baselines"]["bulb-a"])
    previous_release.EventRecord.model_validate(rows["events"]["bulb-a"])
    TypeAdapter(previous_release.CurrentReading).validate_python(rows["current_state"]["bulb-a"])
    previous_release.CommandRecord.model_validate(rows["commands"]["bulb-a"])
