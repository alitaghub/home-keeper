"""Gives every stored row its device kind and drops the `catalog_kind` and `selected_switch`
side tables, once, after backing the database up."""

import sqlite3
from contextlib import closing
from pathlib import Path

# Marks a database as written or upgraded in the current shape.
SCHEMA_VERSION = "1"


class MigrationError(RuntimeError):
    """A row could not be converted. The database is unchanged; the backup stays."""


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.pre-forward-only.bak")


def upgrade(path: Path) -> None:
    """Back up and migrate a database an earlier release wrote. Leaves a new or already
    migrated database alone."""
    with closing(sqlite3.connect(path, timeout=10, isolation_level=None)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if (
            "metadata" not in tables
            or db.execute("SELECT 1 FROM metadata WHERE key='schema_version'").fetchone()
        ):
            return
        # The backup API reads through SQLite, so it includes rows still in the WAL file.
        with closing(sqlite3.connect(backup_path(path))) as backup:
            db.backup(backup)
        db.execute("BEGIN IMMEDIATE")
        try:
            _migrate(db, tables)
            db.execute("INSERT INTO metadata VALUES('schema_version', ?)", (SCHEMA_VERSION,))
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def _migrate(db: sqlite3.Connection, tables: set[str]) -> None:
    if "catalog_kind" in tables:
        _run(
            db,
            "catalog",
            "UPDATE catalog SET data=json_set(data, '$.device_kind', k.device_kind) "
            "FROM catalog_kind k WHERE k.id=catalog.id",
            "DROP TABLE catalog_kind",
        )
        _require(db, "catalog", "json_type(data) IS NOT 'object'")
    if "selection" in tables:
        columns = {row[1] for row in db.execute("PRAGMA table_info(selection)")}
        if "device_kind" not in columns:
            _run(db, "selection", "ALTER TABLE selection ADD COLUMN device_kind TEXT")
        # The kind each device was selected as; the catalog names it unless the device left.
        _run(
            db,
            "selection",
            "UPDATE selection SET device_kind=json_extract(c.data, '$.device_kind') "
            "FROM catalog c WHERE c.id=selection.id",
        )
    if "selected_switch" in tables:
        _run(
            db,
            "selection",
            "UPDATE selection SET device_kind='switch' "
            "WHERE id IN (SELECT id FROM selected_switch)",
            "DROP TABLE selected_switch",
        )
    # Every row stored without a kind is a light's.
    for table in ("current_state", "baselines", "commands"):
        if table in tables:
            _run(
                db,
                table,
                f"UPDATE {table} SET data=json_set(data, '$.device_kind', 'light') "  # noqa: S608 -- fixed table names
                "WHERE json_extract(data, '$.device_kind') IS NULL",
            )
            _require(db, table, "json_extract(data, '$.device_kind') IS NULL")
    if "events" in tables:
        # json_set leaves an event's null old or new state as it is.
        _run(
            db,
            "events",
            "UPDATE events SET data=json_set(data, '$.device_kind', 'light', "
            "'$.old.device_kind', 'light', '$.new.device_kind', 'light') "
            "WHERE json_extract(data, '$.device_kind') IS NULL",
        )
        # A state is null or an object with its kind.
        _require(
            db,
            "events",
            "json_extract(data, '$.device_kind') IS NULL "
            "OR json_type(data, '$.old') NOT IN ('null', 'object') "
            "OR json_type(data, '$.new') NOT IN ('null', 'object') "
            "OR (json_type(data, '$.old')='object' "
            "AND json_extract(data, '$.old.device_kind') IS NULL) "
            "OR (json_type(data, '$.new')='object' "
            "AND json_extract(data, '$.new.device_kind') IS NULL)",
        )


def _run(db: sqlite3.Connection, table: str, *statements: str) -> None:
    try:
        for statement in statements:
            db.execute(statement)
    except sqlite3.Error as error:
        raise MigrationError(f"Could not migrate the {table} table: {error}") from error


def _require(db: sqlite3.Connection, table: str, unconverted: str) -> None:
    """Fail on a row the update could not convert, such as one that is not a JSON object."""
    try:
        row = db.execute(f"SELECT 1 FROM {table} WHERE {unconverted} LIMIT 1").fetchone()  # noqa: S608 -- fixed SQL fragments
    except sqlite3.Error as error:
        raise MigrationError(f"Could not migrate the {table} table: {error}") from error
    if row is not None:
        raise MigrationError(f"Could not migrate the {table} table: a row cannot be converted")
