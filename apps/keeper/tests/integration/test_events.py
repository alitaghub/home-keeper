"""Change events: idempotent ingestion, ordering, scope, retention and paging."""

import sqlite3
from contextlib import closing
from datetime import timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
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


def test_event_replay_ordering_selection_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"

    def app() -> FastAPI:
        return create_app(path, TOKEN, now=lambda: NOW, reader=catalog_reader())

    with TestClient(app()) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post("/internal/baselines", json=observation(revision), headers=AUTH)
        payload = event(revision)
        response = client.post("/internal/events", json=payload, headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["recorded"]
        assert client.post("/internal/events", json=payload, headers=AUTH).json()["duplicate"]
        late = payload | {
            "id": "late",
            "event_time": (NOW - timedelta(seconds=10)).isoformat(),
            "old": {"state": "on"},
            "new": {"state": "off"},
        }
        assert client.post("/internal/events", json=late, headers=AUTH).is_success
        assert recorded_current(path)["state"] == "on"
        unchanged = payload | {"id": "unchanged", "old": {"state": "on", "brightness": 100}}
        assert not client.post("/internal/events", json=unchanged, headers=AUTH).json()["recorded"]
        select(client, selected=False)
        assert (
            client.post(
                "/internal/events", json=payload | {"id": "removed"}, headers=AUTH
            ).status_code
            == 409
        )
        events = client.get("/api/events").json()["events"]
        assert [entry["id"] for entry in events] == ["change-1", "late"]
        assert events[0]["persisted_at"] == NOW.isoformat()
    with TestClient(app()) as client:
        assert client.get("/api/events").json()["events"] == events


def test_resent_event_under_newer_revision_is_a_duplicate_of_the_stored_record(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    with TestClient(
        create_app(tmp_path / "resend.sqlite", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post("/internal/baselines", json=observation(revision), headers=AUTH)
        assert client.post("/internal/events", json=event(revision), headers=AUTH).json()[
            "recorded"
        ]
        stored = client.get("/api/events").json()["events"]
        current = recorded_current(tmp_path / "resend.sqlite")
        newer = select(client, "bulb-b")
        assert newer > revision
        clock[0] += timedelta(seconds=5)
        response = client.post("/internal/events", json=event(newer), headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json() == {"accepted": True, "recorded": False, "duplicate": True}
        assert client.get("/api/events").json()["events"] == stored
        assert stored[0]["selection_revision"] == revision
        assert recorded_current(tmp_path / "resend.sqlite") == current


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


def test_retention_preserves_selection_current_state_and_validates_scope(tmp_path: Path) -> None:
    clock = [NOW]
    with TestClient(
        create_app(
            tmp_path / "retention.sqlite",
            TOKEN,
            now=lambda: clock[0],
            retention_days=1,
            reader=catalog_reader(),
        )
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post("/internal/baselines", json=observation(revision), headers=AUTH)
        client.post("/internal/events", json=event(revision), headers=AUTH)
        clock[0] += timedelta(days=2)
        result = client.get("/api/events").json()
        assert result["events"] == []
        assert result["coverage"] == "unknown"
        assert result["retained_since"] == (clock[0] - timedelta(days=1)).isoformat()
        assert client.get("/api/selection").json()["entity_ids"] == ["bulb-a"]
        assert recorded_current(tmp_path / "retention.sqlite")["state"] == "on"
        assert (
            client.get("/api/baselines", params={"entity_id": "bulb-a"}).json()["baselines"] == []
        )
        for params in (
            {"limit": 101},
            {"entity_id": "<script>"},
            {"cursor": "oops"},
            {"start": "2026-01-01"},
            {"source_id": "other"},
        ):
            assert client.get("/api/events", params=params).status_code == 422


def test_bounded_pagination_and_invalid_event_fields(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "pages.sqlite", TOKEN, now=lambda: NOW, reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        for identity in ("a", "b", "c"):
            assert client.post(
                "/internal/events", json=event(revision, identity), headers=AUTH
            ).is_success
        page = client.get("/api/events", params={"limit": 2}).json()
        assert len(page["events"]) == 2
        assert page["next_cursor"]
        second = client.get(
            "/api/events", params={"limit": 2, "cursor": page["next_cursor"]}
        ).json()
        assert len(second["events"]) == 1
        assert second["next_cursor"] is None
        for new in (
            {"state": "<script>"},
            {"state": "on", "rgb_color": [1, 2, 999]},
            {"state": "on", "xy_color": [0.2]},
            {"state": "on", "hs_color": [361, 50]},
            {"state": "on", "user_id": "private"},
        ):
            assert (
                client.post(
                    "/internal/events", json=event(revision, "invalid", new=new), headers=AUTH
                ).status_code
                == 422
            )
        assert client.post("/internal/events", json=event(revision), headers={}).status_code == 401


def test_attribute_only_events_and_source_ordering_against_reconciliation(tmp_path: Path) -> None:
    clock = [NOW]
    with TestClient(
        create_app(tmp_path / "order.sqlite", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post(
            "/internal/baselines",
            json=observation(revision, last_updated=NOW.isoformat()),
            headers=AUTH,
        )
        brightness = event(revision, "brightness", old={"state": "on", "brightness": 50})
        assert client.post("/internal/events", json=brightness, headers=AUTH).json()["recorded"]
        clock[0] += timedelta(seconds=5)
        snapshot = observation(
            revision, clock[0].isoformat(), state="off", last_updated=clock[0].isoformat()
        )
        assert client.post("/internal/baselines", json=snapshot, headers=AUTH).is_success
        late = event(
            revision,
            "late-color",
            old={"state": "off"},
            new={"state": "on", "xy_color": [0.2, 0.3]},
            received_at=clock[0].isoformat(),
        )
        assert client.post("/internal/events", json=late, headers=AUTH).json()["recorded"]
        assert recorded_current(tmp_path / "order.sqlite")["state"] == "off"
        timestamp_only = event(
            revision,
            "timestamp",
            old={"state": "on", "last_updated": NOW.isoformat()},
            new={"state": "on", "last_updated": clock[0].isoformat()},
            received_at=clock[0].isoformat(),
        )
        assert not client.post("/internal/events", json=timestamp_only, headers=AUTH).json()[
            "recorded"
        ]
        assert {e["id"] for e in client.get("/api/events").json()["events"]} == {
            "brightness",
            "late-color",
        }
        conflict = brightness | {"new": {"state": "off"}}
        assert client.post("/internal/events", json=conflict, headers=AUTH).status_code == 409
        for invalid in (
            {"source_id": "home_assistant"},
            {"entity_id": "bulb-b"},
            {"entity_id": "room"},
            {"selection_revision": revision + 1},
        ):
            assert (
                client.post(
                    "/internal/events", json=brightness | invalid | {"id": "denied"}, headers=AUTH
                ).status_code
                == 409
            )


def test_oversized_event_is_rejected_before_validation_without_echo(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "large.sqlite", TOKEN, now=lambda: NOW, reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        payload = event(revision) | {"untrusted": "private-sentinel" * 100000}
        result = client.post("/internal/events", json=payload, headers=AUTH)
        assert result.status_code == 413
        assert "private-sentinel" not in result.text
        assert client.get("/api/events").json()["events"] == []
        assert client.post("/internal/events", json=event(revision), headers=AUTH).is_success


def test_observed_removal_makes_current_state_unknown_and_numeric_fields_are_strict(
    tmp_path: Path,
) -> None:
    with TestClient(
        create_app(tmp_path / "removal.sqlite", TOKEN, now=lambda: NOW, reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        client.post("/internal/events", json=event(revision), headers=AUTH)
        removed = event(revision, "removed", old={"state": "on"}, new=None)
        assert client.post("/internal/events", json=removed, headers=AUTH).json()["recorded"]
        current = recorded_current(tmp_path / "removal.sqlite")
        assert current["state"] == "unknown"
        assert current["kind"] == "event"
        assert client.get("/api/events").json()["events"][0]["new"] is None
        for attributes in (
            {"brightness": True},
            {"brightness": "20"},
            {"rgb_color": [True, 2, 3]},
            {"rgb_color": [1.0, 2, 3]},
            {"hs_color": ["10", 20]},
            {"xy_color": [False, 0.2]},
            {"color_temp_kelvin": "2700"},
        ):
            payload = event(revision, "invalid-number", new={"state": "on", **attributes})
            assert client.post("/internal/events", json=payload, headers=AUTH).status_code == 422


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


def test_offset_ordering_and_timestamp_only_snapshots(tmp_path: Path) -> None:
    clock = [NOW + timedelta(seconds=5)]
    with TestClient(
        create_app(tmp_path / "review.sqlite", TOKEN, now=lambda: clock[0], reader=catalog_reader())
    ) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        revision = select(client)
        latest = NOW + timedelta(microseconds=100)
        payload = event(
            revision,
            "newest",
            event_time=latest.isoformat(),
            old={"state": "on"},
            new={"state": "off"},
        )
        assert client.post("/internal/events", json=payload, headers=AUTH).status_code == 422
        payload["received_at"] = clock[0].isoformat()
        assert client.post("/internal/events", json=payload, headers=AUTH).is_success
        older = event(
            revision,
            "older-offset",
            event_time=NOW.astimezone(timezone(timedelta(hours=2))).isoformat(),
            received_at=clock[0].isoformat(),
        )
        assert client.post("/internal/events", json=older, headers=AUTH).is_success
        assert recorded_current(tmp_path / "review.sqlite")["state"] == "off"
        # An event captured during a snapshot read must retain its newer source state.
        clock[0] += timedelta(seconds=1)
        snapshot = observation(
            revision,
            clock[0].isoformat(),
            last_updated=NOW.isoformat(),
            collection_started_at=(NOW + timedelta(seconds=4)).isoformat(),
        )
        assert client.post("/internal/baselines", json=snapshot, headers=AUTH).is_success
        assert recorded_current(tmp_path / "review.sqlite")["state"] == "off"
        assert [x["id"] for x in client.get("/api/events").json()["events"]] == [
            "newest",
            "older-offset",
        ]


def test_scope_pages_and_repeated_receipts(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "scope.sqlite", TOKEN, now=lambda: NOW, reader=catalog_reader())
    ) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        select(client)
        revision = select(client, "bulb-b")
        for identity, entity in (("a-first", "bulb-a"), ("b", "bulb-b"), ("a-last", "bulb-a")):
            payload = event(revision, identity, entity_id=entity)
            assert client.post("/internal/events", json=payload, headers=AUTH).is_success
            repeat = payload | {"received_at": (NOW - timedelta(seconds=1)).isoformat()}
            # Receipt must still follow event time; retry at the same receipt is sufficient.
            repeat["event_time"] = payload["event_time"]
            assert client.post("/internal/events", json=repeat, headers=AUTH).status_code == 422
            assert client.post("/internal/events", json=payload, headers=AUTH).json()["duplicate"]
        params = {"entity_id": "bulb-a", "source_id": "demo", "limit": 1}
        first = client.get("/api/events", params=params).json()
        second = client.get("/api/events", params=params | {"cursor": first["next_cursor"]}).json()
        assert [x["id"] for x in first["events"] + second["events"]] == ["a-last", "a-first"]
        assert second["next_cursor"] is None
        assert (
            client.get("/api/events", params={"source_id": "home_assistant"}).json()["events"] == []
        )
