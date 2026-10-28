import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter

from home_keeper.migration import SCHEMA_VERSION, upgrade
from home_keeper.models import (
    BaselineRecord,
    CommandRecord,
    CoverageGap,
    CurrentReading,
    DeviceKind,
    Entity,
    EventRecord,
    GapReason,
    HealthReport,
    SelectionSnapshot,
)

current_reading: TypeAdapter[CurrentReading] = TypeAdapter(CurrentReading)
baseline_record: TypeAdapter[BaselineRecord] = TypeAdapter(BaselineRecord)


stored_kind: TypeAdapter[DeviceKind | None] = TypeAdapter(DeviceKind | None)


def stored_json(record: CurrentReading | EventRecord | CommandRecord) -> str:
    """A record's row: the fields it was given, so absent light attributes stay absent."""
    return record.model_dump_json(exclude_unset=True)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # WAL lets readers, such as a long usage report, run without blocking collector writes.
        # The mode is stored in the database file, so setting it once at startup is enough.
        with closing(sqlite3.connect(path, timeout=10)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        upgrade(path)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS catalog(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                -- The kind a device was selected as outlives its catalog row.
                CREATE TABLE IF NOT EXISTS selection(id TEXT PRIMARY KEY, device_kind TEXT);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES('revision', '0');
                CREATE TABLE IF NOT EXISTS baselines(
                    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS baseline_entity ON baselines(entity_id);
                CREATE TABLE IF NOT EXISTS events(
                    source_id TEXT NOT NULL, id TEXT NOT NULL, entity_id TEXT NOT NULL,
                    event_time TEXT NOT NULL, persisted_at TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(source_id, id)
                );
                CREATE INDEX IF NOT EXISTS event_order ON events(event_time, id);
                CREATE INDEX IF NOT EXISTS event_expiration ON events(persisted_at);
                CREATE INDEX IF NOT EXISTS event_scope ON events(entity_id, event_time, id);
                -- One light's newest events, read while a command waits for its result.
                DROP INDEX IF EXISTS event_entity;
                CREATE INDEX IF NOT EXISTS event_receipt_scope ON events(entity_id, persisted_at);
                CREATE TABLE IF NOT EXISTS current_state(
                    entity_id TEXT PRIMARY KEY, source_time TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coverage(
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, detected_at TEXT NOT NULL,
                    recovered_at TEXT, data TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS open_coverage_scope
                    ON coverage(scope) WHERE recovered_at IS NULL;
                CREATE TABLE IF NOT EXISTS confirmed(
                    entity_id TEXT PRIMARY KEY, received_at TEXT NOT NULL,
                    persisted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands(
                    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, sent_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS command_order ON commands(sent_at, id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(current_state)")}
            if "snapshot_started_at" not in columns:
                db.execute("ALTER TABLE current_state ADD COLUMN snapshot_started_at TEXT")
            if "snapshot_received_at" not in columns:
                db.execute("ALTER TABLE current_state ADD COLUMN snapshot_received_at TEXT")
                # A stored snapshot's receipt is a lower bound until the next snapshot arrives.
                db.execute(
                    "UPDATE current_state "
                    "SET snapshot_received_at=json_extract(data, '$.received_at') "
                    "WHERE json_extract(data, '$.kind') != 'event'"
                )
            columns = {row[1] for row in db.execute("PRAGMA table_info(baselines)")}
            if "persisted_at" not in columns:
                db.execute("ALTER TABLE baselines ADD COLUMN persisted_at TEXT")
                db.execute("UPDATE baselines SET persisted_at=json_extract(data, '$.persisted_at')")
            db.execute("CREATE INDEX IF NOT EXISTS baseline_expiration ON baselines(persisted_at)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS baseline_scope ON baselines(entity_id, persisted_at)"
            )
            # A new database starts in the current shape; `upgrade` marks an older one.
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES('schema_version', ?)", (SCHEMA_VERSION,)
            )

    @contextmanager
    def transaction(
        self, mode: Literal["IMMEDIATE", "DEFERRED"] = "IMMEDIATE"
    ) -> Generator[sqlite3.Connection]:
        """IMMEDIATE takes the write lock up front; DEFERRED suits reads that need no lock."""
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute(f"BEGIN {mode}")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def selection(db: sqlite3.Connection) -> SelectionSnapshot:
        return SelectionSnapshot(
            revision=int(
                db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]
            ),
            refresh_revision=int(
                db.execute("SELECT value FROM metadata WHERE key='refresh_revision'").fetchone()[0]
            ),
            entity_ids=[row[0] for row in db.execute("SELECT id FROM selection ORDER BY id")],
        )

    @staticmethod
    def selected_kind(db: sqlite3.Connection, entity_id: str) -> DeviceKind | None:
        """The kind a selected device had in the catalog when it was selected."""
        row = db.execute("SELECT device_kind FROM selection WHERE id=?", (entity_id,)).fetchone()
        return stored_kind.validate_python(row[0]) if row else None

    @staticmethod
    def entities(db: sqlite3.Connection, entity_id: str | None = None) -> list[Entity]:
        rows = db.execute(
            "SELECT data FROM catalog WHERE ? IS NULL OR id = ? ORDER BY id",
            (entity_id, entity_id),
        )
        return [Entity.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def entity(db: sqlite3.Connection, entity_id: str) -> Entity | None:
        return next(iter(Store.entities(db, entity_id)), None)

    @staticmethod
    def current(db: sqlite3.Connection, entity_id: str) -> CurrentReading | None:
        row = db.execute(
            "SELECT data FROM current_state WHERE entity_id=?", (entity_id,)
        ).fetchone()
        return current_reading.validate_json(row[0]) if row else None

    @staticmethod
    def source_entity_id(db: sqlite3.Connection, entity_id: str) -> str | None:
        """The device's newest recorded Home Assistant entity ID, from any stored reading."""
        for table in ("events", "baselines"):
            row = db.execute(
                # Only fixed table names are interpolated; values are bound parameters.
                f"SELECT json_extract(data, '$.source_entity_id') FROM {table} "  # noqa: S608
                "WHERE entity_id=? AND json_extract(data, '$.source_entity_id') IS NOT NULL "
                "ORDER BY rowid DESC LIMIT 1",
                (entity_id,),
            ).fetchone()
            if row:
                return row[0]
        return None

    @staticmethod
    def health(db: sqlite3.Connection) -> HealthReport | None:
        row = db.execute("SELECT value FROM metadata WHERE key='health'").fetchone()
        return HealthReport.model_validate_json(row[0]) if row else None

    @staticmethod
    def cleanup(db: sqlite3.Connection, cutoff: str) -> None:
        db.execute("DELETE FROM events WHERE persisted_at < ?", (cutoff,))
        # The API stamps sent_at with its own clock, like persisted_at.
        db.execute("DELETE FROM commands WHERE sent_at < ?", (cutoff,))
        db.execute(
            "DELETE FROM coverage WHERE recovered_at IS NOT NULL AND recovered_at < ?", (cutoff,)
        )
        db.execute(
            "DELETE FROM current_state WHERE entity_id NOT IN (SELECT id FROM selection) "
            "AND json_extract(data, '$.persisted_at') < ?",
            (cutoff,),
        )
        db.execute(
            "DELETE FROM confirmed WHERE entity_id NOT IN (SELECT id FROM selection) "
            "AND persisted_at < ?",
            (cutoff,),
        )
        # Snapshot receipt time is not retention time: use the API persistence timestamp.
        db.execute("DELETE FROM baselines WHERE persisted_at < ?", (cutoff,))

    @staticmethod
    def last_confirmed(db: sqlite3.Connection, entity_id: str | None = None) -> str | None:
        if entity_id is not None:
            row = db.execute(
                "SELECT persisted_at FROM confirmed WHERE entity_id=?", (entity_id,)
            ).fetchone()
            return row[0] if row else None
        row = db.execute("SELECT MAX(persisted_at) FROM confirmed").fetchone()
        retained = db.execute("SELECT value FROM metadata WHERE key='last_healthy_at'").fetchone()
        health = Store.health(db)
        timestamps: list[str] = [row[0]] if row[0] else []
        if retained:
            timestamps.append(retained[0])
        if health and health.status == "healthy":
            timestamps.append(health.persisted_at)
        return max(timestamps) if timestamps else None

    @classmethod
    def interrupt(
        cls,
        db: sqlite3.Connection,
        source_id: str,
        detected_at: str,
        reason: GapReason,
        entity_id: str | None = None,
    ) -> CoverageGap:
        scope = entity_id or "*"
        existing = db.execute(
            "SELECT data FROM coverage WHERE scope=? AND recovered_at IS NULL", (scope,)
        ).fetchone()
        if existing:
            return CoverageGap.model_validate_json(existing[0])
        gap = CoverageGap.model_validate(
            {
                "id": str(uuid4()),
                "source_id": source_id,
                "entity_ids": [entity_id] if entity_id else cls.selection(db).entity_ids,
                "last_confirmed_at": cls.last_confirmed(db, entity_id),
                "uncertain_start": True,
                "detected_at": detected_at,
                "recovered_at": None,
                "reason": reason,
            }
        )
        db.execute(
            "INSERT INTO coverage VALUES(?, ?, ?, NULL, ?)",
            (gap.id, scope, detected_at, gap.model_dump_json()),
        )
        return gap

    @staticmethod
    def open_gaps(db: sqlite3.Connection, entity_id: str | None = None) -> list[CoverageGap]:
        if entity_id is None:
            rows = db.execute("SELECT data FROM coverage WHERE recovered_at IS NULL")
        else:
            rows = db.execute(
                "SELECT data FROM coverage WHERE recovered_at IS NULL AND scope IN ('*', ?)",
                (entity_id,),
            )
        return [CoverageGap.model_validate_json(row[0]) for row in rows]
