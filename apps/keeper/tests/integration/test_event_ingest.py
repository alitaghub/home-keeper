"""The collector's change events: how the recorder ingests and keeps them.

`/api/events` reads Home Assistant's history; these rules hold until the recorder goes.
"""

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from support.api import (
    AUTH,
    CATALOG,
    NOW,
    TOKEN,
    catalog_reader,
    event,
    observation,
    recorded_current,
    select,
)
from support.recorded_reader import RecordedReader

from home_keeper.api import create_app


def test_resent_event_id_rejects_changed_evidence_and_stale_revisions(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "reject.sqlite", TOKEN, now=lambda: NOW, reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        assert client.post("/internal/events", json=event(revision), headers=AUTH).json()[
            "recorded"
        ]
        newer = select(client, "bulb-b")
        for payload, code in (
            (event(newer, new={"state": "off"}), "event_identity_conflict"),
            (event(revision), "selection_changed"),
        ):
            response = client.post("/internal/events", json=payload, headers=AUTH)
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == code


def test_observed_switch_removal_reports_an_unknown_switch_reading(tmp_path: Path) -> None:
    plug = {
        "id": "plug-a",
        "device_id": "device-plug-a",
        "device_name": "Kettle plug",
        "name": "Kettle",
        "device_kind": "switch",
        "supported": True,
        "reason": None,
    }
    catalog = CATALOG | {"entities": [plug]}
    db = tmp_path / "removal.sqlite"
    # The plug is only in the posted catalog, which the recorded reader serves back.
    with TestClient(create_app(db, TOKEN, now=lambda: NOW, reader=RecordedReader(db))) as client:
        client.post("/internal/catalog", json=catalog, headers=AUTH).raise_for_status()
        revision = select(client, "plug-a")
        removed = event(
            revision,
            "plug-removed",
            entity_id="plug-a",
            source_entity_id="switch.kettle",
            old={"state": "on", "device_kind": "switch"},
            new=None,
        )
        response = client.post("/internal/events", json=removed, headers=AUTH)
        assert response.json()["recorded"], response.text
        current = recorded_current(db, "plug-a")
        assert (current["kind"], current["device_kind"]) == ("event", "switch")
        assert (current["state"], current["availability"]) == ("unknown", "unknown")
        light_fields = {"brightness", "color_mode", "color_temp_kelvin", "hs_color", "xy_color"}
        assert not light_fields & current.keys(), current


def test_legacy_snapshot_migration_compares_timestamp_instants(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    with TestClient(create_app(path, TOKEN, now=lambda: NOW, reader=catalog_reader())) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post(
            "/internal/baselines",
            json=observation(revision, last_updated=NOW.isoformat()),
            headers=AUTH,
        ).raise_for_status()
    # Removing the current-state cache exercises migration from a snapshot-only database.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TABLE current_state")
    with TestClient(create_app(path, TOKEN, now=lambda: NOW, reader=catalog_reader())) as client:
        payload = event(revision, "migration-change", old={"state": "on"}, new={"state": "off"})
        client.post("/internal/events", json=payload, headers=AUTH).raise_for_status()
        assert recorded_current(path)["state"] == "off"


def test_newer_poll_replaces_event_state_after_source_clock_steps_back(tmp_path: Path) -> None:
    clock = [NOW]
    with TestClient(
        create_app(tmp_path / "clock.sqlite", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post(
            "/internal/baselines",
            json=observation(revision, last_updated=NOW.isoformat()),
            headers=AUTH,
        ).raise_for_status()
        client.post("/internal/events", json=event(revision), headers=AUTH).raise_for_status()
        clock[0] += timedelta(seconds=5)
        # Home Assistant's clock stepped back before the light turned off.
        earlier = (NOW - timedelta(minutes=1)).isoformat()
        snapshot = observation(revision, clock[0].isoformat(), state="off", last_updated=earlier)
        client.post("/internal/baselines", json=snapshot, headers=AUTH).raise_for_status()
        current = recorded_current(tmp_path / "clock.sqlite")
        assert current["state"] == "off"
