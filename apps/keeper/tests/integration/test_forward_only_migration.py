"""A database whose light rows lack `device_kind`, with kinds in the `catalog_kind` and
`selected_switch` side tables, is backed up, then migrated once. Fixture rows are literal."""

import json
import shutil
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from support.api import AUTH, NOW, TOKEN
from support.recorded_reader import RecordedReader

from home_keeper.api import create_app
from home_keeper.events import change_event
from home_keeper.models import LightState

# The schema as that release's store created it.
SCHEMA = """
CREATE TABLE catalog(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE catalog_kind(id TEXT PRIMARY KEY, device_kind TEXT NOT NULL);
CREATE TABLE selection(id TEXT PRIMARY KEY);
CREATE TABLE selected_switch(id TEXT PRIMARY KEY);
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE baselines(
    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, data TEXT NOT NULL, persisted_at TEXT
);
CREATE INDEX baseline_entity ON baselines(entity_id);
CREATE TABLE events(
    source_id TEXT NOT NULL, id TEXT NOT NULL, entity_id TEXT NOT NULL,
    event_time TEXT NOT NULL, persisted_at TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(source_id, id)
);
CREATE INDEX event_order ON events(event_time, id);
CREATE INDEX event_expiration ON events(persisted_at);
CREATE INDEX event_scope ON events(entity_id, event_time, id);
CREATE INDEX event_receipt_scope ON events(entity_id, persisted_at);
CREATE TABLE current_state(
    entity_id TEXT PRIMARY KEY, source_time TEXT NOT NULL, data TEXT NOT NULL,
    snapshot_started_at TEXT, snapshot_received_at TEXT
);
CREATE TABLE coverage(
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, detected_at TEXT NOT NULL,
    recovered_at TEXT, data TEXT NOT NULL
);
CREATE UNIQUE INDEX open_coverage_scope ON coverage(scope) WHERE recovered_at IS NULL;
CREATE TABLE confirmed(
    entity_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, persisted_at TEXT NOT NULL
);
CREATE TABLE commands(
    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, sent_at TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX command_order ON commands(sent_at, id);
CREATE INDEX baseline_expiration ON baselines(persisted_at);
CREATE INDEX baseline_scope ON baselines(entity_id, persisted_at);
"""

AT = "2026-10-12T10:00:00+00:00"
CHANGED = "2026-10-12T10:00:01+00:00"
# The light event's ID as that release hashed it; it must still match after the migration.
LIGHT_EVENT = "30aa4353e5d1d50b88083877250fcdade9dda53146197ed2dcb7819ce573fafa"
SWITCH_EVENT = "3d1c0b1f6792de4f817d0b6eeb52b69146c71fe80b97bf49ae50f928b6b8a50e"
SELECTION_REVISION = 3

# A desk lamp and a kettle plug, both monitored with a snapshot, a change and a command each.
# `plug-gone` was selected as a switch, then left discovery: only `selected_switch` knows it.
ROWS: dict[str, list[tuple[str | None, ...]]] = {
    "catalog": [
        (
            "bulb-a",
            '{"id":"bulb-a","device_id":"device-a","device_name":"Desk lamp","name":"Desk",'
            '"supported":true,"reason":null}',
        ),
        (
            "room",
            '{"id":"room","device_id":null,"device_name":null,"name":"Living room",'
            '"supported":false,"reason":"Groups and rooms are not recorded"}',
        ),
        (
            "plug-a",
            '{"id":"plug-a","device_id":"device-plug-a","device_name":"Kettle plug",'
            '"name":"Kettle","supported":true,"reason":null}',
        ),
    ],
    "catalog_kind": [("bulb-a", "light"), ("plug-a", "switch")],
    "selection": [("bulb-a",), ("plug-gone",), ("plug-a",)],
    "selected_switch": [("plug-gone",), ("plug-a",)],
    "metadata": [
        ("revision", str(SELECTION_REVISION)),
        ("source_id", "demo"),
        ("refresh_revision", "0"),
        ("catalog_at", AT),
        ("control_heartbeat_at", "2026-10-12T10:00:02+00:00"),
    ],
    "baselines": [
        (
            "baseline-bulb",
            "bulb-a",
            '{"state":"off","availability":"available","last_changed":"2020-01-01T00:00:00Z",'
            '"last_updated":"2020-01-01T00:00:00Z","last_reported":null,"brightness":null,'
            '"color_mode":null,"color_temp_kelvin":null,"hs_color":null,"xy_color":null,'
            '"rgb_color":null,"rgbw_color":null,"rgbww_color":null,"id":"baseline-bulb",'
            '"kind":"baseline","entity_id":"bulb-a","source_entity_id":"light.desk",'
            '"source_id":"demo","received_at":"2026-10-12T10:00:00Z",'
            '"collection_started_at":null,"persisted_at":"2026-10-12T10:00:00+00:00"}',
            AT,
        ),
        (
            "baseline-plug",
            "plug-a",
            '{"state":"on","availability":"available","last_changed":"2020-01-01T00:00:00Z",'
            '"last_updated":"2020-01-01T00:00:00Z","last_reported":null,"device_kind":"switch",'
            '"id":"baseline-plug","kind":"baseline","entity_id":"plug-a",'
            '"source_entity_id":"switch.kettle","source_id":"demo",'
            '"received_at":"2026-10-12T10:00:00Z","collection_started_at":null,'
            '"persisted_at":"2026-10-12T10:00:00+00:00"}',
            AT,
        ),
    ],
    "events": [
        (
            "demo",
            LIGHT_EVENT,
            "bulb-a",
            CHANGED,
            CHANGED,
            f'{{"id": "{LIGHT_EVENT}", "entity_id": "bulb-a", "source_entity_id": "light.desk",'
            ' "source_id": "demo", "selection_revision": 3,'
            ' "event_time": "2026-10-12T10:00:01Z", "received_at": "2026-10-12T10:00:01Z",'
            ' "old": {"state": "off", "availability": "available"},'
            ' "new": {"brightness": 100, "color_mode": "brightness", "state": "on",'
            ' "availability": "available"}, "persisted_at": "2026-10-12T10:00:01+00:00"}',
        ),
        (
            "demo",
            SWITCH_EVENT,
            "plug-a",
            CHANGED,
            CHANGED,
            f'{{"id": "{SWITCH_EVENT}", "entity_id": "plug-a",'
            ' "source_entity_id": "switch.kettle", "source_id": "demo",'
            ' "selection_revision": 3, "event_time": "2026-10-12T10:00:01Z",'
            ' "received_at": "2026-10-12T10:00:01Z",'
            ' "old": {"state": "on", "availability": "available", "device_kind": "switch"},'
            ' "new": {"state": "off", "availability": "available", "device_kind": "switch"},'
            ' "persisted_at": "2026-10-12T10:00:01+00:00", "device_kind": "switch"}',
        ),
    ],
    "current_state": [
        (
            "bulb-a",
            CHANGED,
            '{"state":"on","availability":"available","brightness":100,'
            f'"color_mode":"brightness","id":"{LIGHT_EVENT}","kind":"event","entity_id":"bulb-a",'
            '"source_entity_id":"light.desk","source_id":"demo",'
            '"event_time":"2026-10-12T10:00:01+00:00","received_at":"2026-10-12T10:00:01+00:00",'
            '"persisted_at":"2026-10-12T10:00:01+00:00"}',
            AT,
            AT,
        ),
        (
            "plug-a",
            CHANGED,
            '{"state":"off","availability":"available","device_kind":"switch",'
            f'"id":"{SWITCH_EVENT}","kind":"event","entity_id":"plug-a",'
            '"source_entity_id":"switch.kettle","source_id":"demo",'
            '"event_time":"2026-10-12T10:00:01+00:00","received_at":"2026-10-12T10:00:01+00:00",'
            '"persisted_at":"2026-10-12T10:00:01+00:00"}',
            AT,
            AT,
        ),
    ],
    "confirmed": [("bulb-a", CHANGED, CHANGED), ("plug-a", CHANGED, CHANGED)],
    "commands": [
        (
            "command-bulb",
            "bulb-a",
            "2026-10-12T10:00:02+00:00",
            '{"id":"command-bulb","entity_id":"bulb-a","command":{"action":"off"},'
            '"sender":{"kind":"telegram","id":424242,"name":"Ali <b>Telegram</b>"},'
            '"sent_at":"2026-10-12T10:00:02+00:00","outcome":"not_observed","event_id":null}',
        ),
        (
            "command-plug",
            "plug-a",
            "2026-10-12T10:00:03+00:00",
            '{"id":"command-plug","entity_id":"plug-a","device_kind":"switch",'
            '"command":{"action":"on"},'
            '"sender":{"kind":"telegram","id":424242,"name":"Ali <b>Telegram</b>"},'
            '"sent_at":"2026-10-12T10:00:03+00:00","outcome":"not_observed","event_id":null}',
        ),
    ],
}
SIDE_TABLES = {"catalog_kind", "selected_switch"}
# What base e91d9c9 returned for this fixture, before the migration existed. Never regenerate
# it with the migrating code: the migration must leave every body as it was.
BEFORE = Path(__file__).parent / "snapshots" / "forward_only_migration.json"
STARTED = NOW + timedelta(minutes=10)
ROUTES: dict[str, tuple[str, dict[str, str]]] = {
    "catalog": ("/api/catalog", {}),
    "monitored": ("/api/monitored", {}),
    "commands": ("/api/commands", {}),
    "usage": (
        "/api/usage",
        {"start": (NOW - timedelta(hours=1)).isoformat(), "end": STARTED.isoformat()},
    ),
}

type Contents = dict[str, list[tuple[object, ...]]]


def write_todays_database(path: Path, *extra: str) -> None:
    """The fixture rows, then any `extra` SQL, committed and checkpointed into the main file."""
    with closing(sqlite3.connect(path)) as db:
        # The store's journal mode, which a database of that release already has.
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        with db:
            for table, rows in ROWS.items():
                for row in rows:
                    db.execute(
                        f"INSERT INTO {table} VALUES({','.join('?' * len(row))})",
                        row,
                    )
            for statement in extra:
                db.execute(statement)


def backup_of(path: Path) -> Path:
    return path.with_name(path.name + ".pre-forward-only.bak")


def contents(path: Path) -> Contents:
    """Every table's rows and the schema, in a stable order."""
    with closing(sqlite3.connect(path)) as db:
        return read_contents(db)


def read_contents(db: sqlite3.Connection) -> Contents:
    tables: list[str] = [
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    result: Contents = {
        "sqlite_master": sorted(db.execute("SELECT type, name, sql FROM sqlite_master"), key=str)
    }
    for table in tables:
        result[table] = sorted(db.execute(f"SELECT * FROM {table}"), key=str)  # noqa: S608 -- names from sqlite_master
    return result


def metadata(path: Path, key: str) -> str | None:
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def stored(path: Path, table: str) -> dict[str, dict[str, Any]]:
    """Each row's decoded `data` in `table`, by entity ID."""
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(f"SELECT entity_id, data FROM {table}").fetchall()  # noqa: S608 -- fixed table names from the test
    return {entity: json.loads(data) for entity, data in rows}


# Public fields ticket 01 removed, or added in their place, by route. The migration's
# promise covers the fields both releases return.
CHANGED_FIELDS: dict[str, set[str]] = {
    "catalog": {"refreshed_at", "freshness"},
    # A device gone from Home Assistant is now `missing`, not unsupported with a reason.
    "monitored": {
        "baseline",
        "freshness",
        "confirmed_at",
        "source_entity_id",
        "state",
        "missing",
        "supported",
        "reason",
    },
    "commands": {"event_id"},
}


def without_changed_fields(bodies: dict[str, Any]) -> dict[str, Any]:
    """`bodies` without the fields in CHANGED_FIELDS, at the top level or in each record."""

    def strip(value: object, fields: set[str]) -> object:
        if isinstance(value, dict):
            record = cast(dict[str, object], value)
            return {key: strip(item, fields) for key, item in record.items() if key not in fields}
        if isinstance(value, list):
            return [strip(item, fields) for item in cast(list[object], value)]
        return value

    return {name: strip(body, CHANGED_FIELDS.get(name, set())) for name, body in bodies.items()}


@contextmanager
def started(path: Path, at: datetime = STARTED) -> Generator[TestClient]:
    with TestClient(create_app(path, TOKEN, now=lambda: at, reader=RecordedReader(path))) as client:
        yield client


def test_a_migrated_database_reads_back_as_before_with_every_kind_stored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "home.sqlite"
    write_todays_database(path)

    with started(path) as client:
        bodies = {
            name: client.get(route, params=params).json()
            for name, (route, params) in ROUTES.items()
        }

    # The base response has no usage periods, so the comparison drops that additive field.
    for device in bodies["usage"]["devices"]:
        assert device.pop("periods") is not None
    before = json.loads(BEFORE.read_text())
    # `/api/events` now reads Home Assistant's history; the events table is checked below.
    del before["events"]
    assert without_changed_fields(bodies) == without_changed_fields(before)
    assert not SIDE_TABLES & set(contents(path)), "the side tables must be gone"
    assert metadata(path, "schema_version") is not None
    for table in ("current_state", "baselines", "events", "commands"):
        rows = stored(path, table)
        assert rows["bulb-a"].get("device_kind") == "light", (table, rows["bulb-a"])
        assert rows["plug-a"].get("device_kind") == "switch", (table, rows["plug-a"])
    for state in ("old", "new"):
        assert stored(path, "events")["bulb-a"][state].get("device_kind") == "light"
    with closing(sqlite3.connect(path)) as db:
        [lamp] = db.execute("SELECT data FROM catalog WHERE id='bulb-a'").fetchone()
    assert json.loads(lamp).get("device_kind") == "light"


def test_a_new_database_starts_in_the_new_shape_already_migrated(tmp_path: Path) -> None:
    path = tmp_path / "new.sqlite"
    with started(path):
        pass
    assert metadata(path, "schema_version") is not None
    assert not SIDE_TABLES & set(contents(path)), "the side tables must not be created"


def test_the_backup_holds_a_row_committed_only_to_the_write_ahead_log(tmp_path: Path) -> None:
    path = tmp_path / "home.sqlite"
    write_todays_database(path)
    # An open connection with checkpoints off keeps this commit in `home.sqlite-wal` only.
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        with writer:
            writer.execute("INSERT INTO selection VALUES('in-wal-only')")
        main_file_only = tmp_path / "main-only.sqlite"
        shutil.copyfile(path, main_file_only)
        assert ("in-wal-only",) not in contents(main_file_only)["selection"]
        before = read_contents(writer)
        assert ("in-wal-only",) in before["selection"]

        with started(path):
            pass
    finally:
        writer.close()

    backup = backup_of(path)
    assert backup.exists(), "no backup next to the database"
    assert contents(backup) == before


def test_a_second_start_neither_migrates_again_nor_touches_the_backup(tmp_path: Path) -> None:
    path = tmp_path / "home.sqlite"
    write_todays_database(path)
    with started(path):
        pass
    backup = backup_of(path)
    assert backup.exists(), "no backup next to the database"
    taken = (backup.read_bytes(), backup.stat().st_mtime_ns)
    version = metadata(path, "schema_version")
    # Only a migration would drop this table again.
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE catalog_kind(id TEXT PRIMARY KEY, device_kind TEXT NOT NULL)")

    with started(path, STARTED + timedelta(minutes=1)):
        pass

    assert (backup.read_bytes(), backup.stat().st_mtime_ns) == taken
    assert metadata(path, "schema_version") == version
    assert "catalog_kind" in contents(path)


# Truncated JSON fails the update itself; a JSON array passes it unchanged and is caught only
# by the check that every row now has a kind.
@pytest.mark.parametrize("data", ['{"id": "command-broken"', "[]"])
def test_a_row_that_cannot_be_converted_leaves_the_database_as_it_was(
    tmp_path: Path, data: str
) -> None:
    path = tmp_path / "home.sqlite"
    # Current state and baselines are migrated before commands, so they have already changed
    # when the migration reaches this row.
    write_todays_database(
        path,
        "INSERT INTO commands VALUES("
        f"'command-broken', 'bulb-a', '2026-10-12T10:00:04+00:00', '{data}')",
    )
    before = contents(path)

    # The failing table's name in the message is the contract, not an exception type.
    with pytest.raises(Exception, match="commands"):
        create_app(path, TOKEN, now=lambda: STARTED, reader=RecordedReader(path))

    assert contents(path) == before
    assert metadata(path, "schema_version") is None
    assert backup_of(path).exists(), "the backup must stay after a failed migration"


@pytest.mark.parametrize(
    ("table", "damage"),
    [
        ("catalog", "UPDATE catalog SET data='[]' WHERE id='bulb-a'"),
        ("events", "UPDATE events SET data=json_set(data, '$.old', json('[]'))"),
        ("events", "UPDATE events SET data=json_set(data, '$.new', json('[]'))"),
    ],
)
def test_a_catalog_entry_or_event_state_that_is_not_an_object_stops_the_migration(
    tmp_path: Path, table: str, damage: str
) -> None:
    path = tmp_path / "home.sqlite"
    write_todays_database(path, damage)
    before = contents(path)

    with pytest.raises(Exception, match=table):
        create_app(path, TOKEN, now=lambda: STARTED, reader=RecordedReader(path))

    assert contents(path) == before
    assert metadata(path, "schema_version") is None


def test_a_light_event_recorded_before_the_upgrade_is_a_duplicate_when_sent_again(
    tmp_path: Path,
) -> None:
    path = tmp_path / "home.sqlite"
    write_todays_database(path)
    # The collector's change for the same Home Assistant states, rebuilt after the upgrade.
    again = change_event(
        source_id="demo",
        entity_id="bulb-a",
        source_entity_id="light.desk",
        revision=SELECTION_REVISION,
        event_time=datetime.fromisoformat(CHANGED),
        received_at=STARTED,
        old=LightState.model_validate({"state": "off"}),
        new=LightState.model_validate(
            {"state": "on", "brightness": 100, "color_mode": "brightness"}
        ),
    )
    assert again.id == LIGHT_EVENT

    with started(path) as client:
        migrated = stored(path, "events")["bulb-a"]
        assert migrated.get("device_kind") == "light", migrated
        response = client.post(
            "/internal/events",
            content=again.model_dump_json(),
            headers=AUTH | {"Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["duplicate"] is True

    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT data FROM events WHERE entity_id='bulb-a'").fetchall()
    assert [json.loads(data) for (data,) in rows] == [migrated]
