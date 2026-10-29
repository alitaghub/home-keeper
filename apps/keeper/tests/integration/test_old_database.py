"""The API starts on a database it does not recognize by replacing it with an empty one.

Home Keeper now reads device history from Home Assistant and keeps only the selection and
the command audit. A database from an older release, or any other file at the path, is
deleted with its WAL files; the user selects their devices again.
"""

import logging
import shutil
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from support.api import BROWSER, NOW, TOKEN, app_for, catalog_reader

from home_keeper.api import create_app

REMOVED = "Removed the old Home Keeper database"
SELECT_AGAIN = "select your devices again"
TABLES = {"selection", "commands", "metadata"}
# Text only the old database holds, in rows that live in its WAL file at copy time.
OLD_MARKER = "old-home-marker"

# The schema main's store created, with the collector's copied tables.
MAIN_SCHEMA = """
CREATE TABLE catalog(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE selection(id TEXT PRIMARY KEY, device_kind TEXT);
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE baselines(
    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, data TEXT NOT NULL, persisted_at TEXT
);
CREATE TABLE events(
    source_id TEXT NOT NULL, id TEXT NOT NULL, entity_id TEXT NOT NULL,
    event_time TEXT NOT NULL, persisted_at TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY(source_id, id)
);
CREATE TABLE current_state(
    entity_id TEXT PRIMARY KEY, source_time TEXT NOT NULL, data TEXT NOT NULL,
    snapshot_started_at TEXT, snapshot_received_at TEXT
);
CREATE TABLE coverage(
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, detected_at TEXT NOT NULL,
    recovered_at TEXT, data TEXT NOT NULL
);
CREATE TABLE confirmed(
    entity_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, persisted_at TEXT NOT NULL
);
CREATE TABLE commands(
    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, sent_at TEXT NOT NULL, data TEXT NOT NULL
);
INSERT INTO metadata VALUES('revision', '2'), ('refresh_revision', '0'),
    ('schema_version', '1'), ('source_id', 'demo');
"""

MAIN_ROWS = f"""
INSERT INTO selection VALUES('bulb-a', 'light'), ('{OLD_MARKER}-plug', 'switch');
INSERT INTO baselines VALUES('baseline-1', 'bulb-a', '{{"state":"{OLD_MARKER}"}}', '{NOW}');
INSERT INTO events VALUES('demo', 'change-1', 'bulb-a', '{NOW}', '{NOW}',
    '{{"new":{{"state":"{OLD_MARKER}"}}}}');
INSERT INTO current_state VALUES('bulb-a', '{NOW}', '{{"state":"{OLD_MARKER}"}}', NULL, NULL);
INSERT INTO coverage VALUES('gap-1', '*', '{NOW}', NULL, '{{"reason":"{OLD_MARKER}"}}');
"""


def write_main_database(path: Path, scratch: Path) -> None:
    """Copy a live WAL-mode database from main, so `path` comes with `-wal` and `-shm` files
    that hold rows the main file does not have yet."""
    scratch.mkdir()
    source = scratch / "main.sqlite"
    with closing(sqlite3.connect(source)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(MAIN_SCHEMA)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.executescript(MAIN_ROWS)
        for suffix in ("", "-wal", "-shm"):
            shutil.copyfile(f"{source}{suffix}", f"{path}{suffix}")
    assert OLD_MARKER.encode() in Path(f"{path}-wal").read_bytes()


def tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as db:
        return {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }


def schema_marker(path: Path) -> str | None:
    with closing(sqlite3.connect(path)) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
    return row[0] if row else None


def removal_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and REMOVED in record.getMessage()
    ]


def assert_replaced(path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert tables(path) == TABLES
    assert schema_marker(path) == "ha-history-1"
    [line] = removal_lines(caplog)
    assert SELECT_AGAIN in line
    for suffix in ("", "-wal", "-shm"):
        file = Path(f"{path}{suffix}")
        assert not file.exists() or OLD_MARKER.encode() not in file.read_bytes(), file


def test_a_database_from_main_is_deleted_with_its_wal_and_the_api_starts_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "home.sqlite"
    write_main_database(path, tmp_path / "main")

    with caplog.at_level(logging.WARNING), TestClient(app_for(path)) as client:
        assert client.get("/api/selection").json() == {"entity_ids": []}

    assert_replaced(path, caplog)


def test_a_second_start_keeps_the_new_database_and_its_selection(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "home.sqlite"
    write_main_database(path, tmp_path / "main")
    with TestClient(app_for(path)) as client:
        selected = client.put("/api/selection/bulb-a", json={"selected": True}, headers=BROWSER)
        assert selected.is_success, selected.text
    caplog.clear()

    with caplog.at_level(logging.WARNING), TestClient(app_for(path)) as client:
        assert client.get("/api/selection").json() == {"entity_ids": ["bulb-a"]}

    assert removal_lines(caplog) == []
    assert tables(path) == TABLES


def test_a_file_that_is_not_a_database_is_replaced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "home.sqlite"
    path.write_bytes(b"not a Home Keeper database\n" * 100)

    with caplog.at_level(logging.WARNING), TestClient(app_for(path)) as client:
        assert client.get("/api/selection").json() == {"entity_ids": []}

    assert tables(path) == TABLES
    assert schema_marker(path) == "ha-history-1"
    [line] = removal_lines(caplog)
    assert SELECT_AGAIN in line


def test_a_new_database_is_created_without_a_removal_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "new" / "home.sqlite"

    with caplog.at_level(logging.WARNING), TestClient(app_for(path)):
        pass

    assert tables(path) == TABLES
    assert removal_lines(caplog) == []


def test_commands_older_than_the_retention_window_are_deleted(tmp_path: Path) -> None:
    path = tmp_path / "home.sqlite"
    app = create_app(
        db_path=path,
        service_token=TOKEN,
        now=lambda: NOW,
        reader=catalog_reader(),
        retention_days=1,
    )
    old = (NOW - timedelta(days=1, seconds=1)).isoformat()
    recent = (NOW - timedelta(hours=23)).isoformat()
    with closing(sqlite3.connect(path)) as db, db:
        db.executemany(
            "INSERT INTO commands VALUES(?, 'bulb-a', ?, '{}')",
            [("old-command", old), ("recent-command", recent)],
        )

    with TestClient(app) as client:
        assert client.get("/api/control").is_success

    with closing(sqlite3.connect(path)) as db:
        kept = [row[0] for row in db.execute("SELECT id FROM commands")]
    assert kept == ["recent-command"]
