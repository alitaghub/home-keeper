"""Readings stored before Home Keeper recorded plugs and switches still read as lights."""

import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient
from support.api import AUTH, CATALOG, app_for, select

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
    # History is read as lights, not rewritten.
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT data FROM events").fetchone()[0] == EVENT
        assert db.execute("SELECT data FROM baselines").fetchone()[0] == BASELINE
