import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, TypedDict
from uuid import uuid4

from pydantic import TypeAdapter

from home_observer.models import CurrentReading, Entity, EventReading, HealthReport


class SelectionState(TypedDict):
    revision: int
    refresh_revision: int
    entity_ids: list[str]


class GapRecord(TypedDict):
    id: str
    source_id: str
    entity_ids: list[str]
    last_confirmed_at: str | None
    uncertain_start: bool
    detected_at: str
    recovered_at: str | None
    reason: str


gap_record = TypeAdapter(GapRecord)
current_reading: TypeAdapter[CurrentReading] = TypeAdapter(CurrentReading)


class SnapshotRead(NamedTuple):
    """When an authoritative HTTP state read started and when its result arrived."""

    started_at: datetime
    received_at: datetime


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS catalog(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS selection(id TEXT PRIMARY KEY);
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
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(current_state)")}
            if "snapshot_started_at" not in columns:
                db.execute("ALTER TABLE current_state ADD COLUMN snapshot_started_at TEXT")
            columns = {row[1] for row in db.execute("PRAGMA table_info(baselines)")}
            if "persisted_at" not in columns:
                db.execute("ALTER TABLE baselines ADD COLUMN persisted_at TEXT")
                db.execute("UPDATE baselines SET persisted_at=json_extract(data, '$.persisted_at')")
            db.execute("CREATE INDEX IF NOT EXISTS baseline_expiration ON baselines(persisted_at)")

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def selection(db: sqlite3.Connection) -> SelectionState:
        return {
            "revision": int(
                db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]
            ),
            "refresh_revision": int(
                db.execute("SELECT value FROM metadata WHERE key='refresh_revision'").fetchone()[0]
            ),
            "entity_ids": [row[0] for row in db.execute("SELECT id FROM selection ORDER BY id")],
        }

    @staticmethod
    def entity(db: sqlite3.Connection, entity_id: str) -> Entity | None:
        row = db.execute("SELECT data FROM catalog WHERE id=?", (entity_id,)).fetchone()
        return Entity.model_validate_json(row[0]) if row else None

    @staticmethod
    def current(db: sqlite3.Connection, entity_id: str) -> CurrentReading | None:
        row = db.execute(
            "SELECT data FROM current_state WHERE entity_id=?", (entity_id,)
        ).fetchone()
        return current_reading.validate_json(row[0]) if row else None

    @staticmethod
    def health(db: sqlite3.Connection) -> HealthReport | None:
        row = db.execute("SELECT value FROM metadata WHERE key='health'").fetchone()
        return HealthReport.model_validate_json(row[0]) if row else None

    @staticmethod
    def cleanup(db: sqlite3.Connection, cutoff: str) -> None:
        db.execute("DELETE FROM events WHERE persisted_at < ?", (cutoff,))
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
    def snapshot_can_replace(
        db: sqlite3.Connection, entity_id: str, source_time: datetime, snapshot: SnapshotRead
    ) -> bool:
        started_at, received_at = snapshot
        previous = db.execute(
            "SELECT source_time, data FROM current_state WHERE entity_id=?", (entity_id,)
        ).fetchone()
        if previous is None:
            return True
        value = current_reading.validate_json(previous["data"])
        receipt = datetime.fromisoformat(value.received_at)
        if received_at < receipt:
            return False
        # Events captured during the HTTP read may be newer than its sampled state.
        return not (
            isinstance(value, EventReading)
            and receipt >= started_at
            and datetime.fromisoformat(previous["source_time"]) > source_time
        )

    @classmethod
    def update_current(
        cls,
        db: sqlite3.Connection,
        entity_id: str,
        source_time: str,
        data: CurrentReading,
        *,
        snapshot: SnapshotRead | None = None,
    ) -> None:
        previous = db.execute(
            "SELECT source_time, snapshot_started_at FROM current_state WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        instant = datetime.fromisoformat(source_time).astimezone(UTC)
        boundary = previous["snapshot_started_at"] if previous else None
        if snapshot is not None:
            if not cls.snapshot_can_replace(db, entity_id, instant, snapshot):
                db.execute(
                    "UPDATE current_state SET snapshot_started_at=? WHERE entity_id=?",
                    (snapshot.started_at.isoformat(), entity_id),
                )
                return
            boundary = snapshot.started_at.isoformat()
        else:
            # Keep delayed observations in history without crossing a newer HTTP read boundary.
            if (
                boundary
                and isinstance(data, EventReading)
                and datetime.fromisoformat(data.received_at) < datetime.fromisoformat(boundary)
            ):
                return
            if previous and instant < datetime.fromisoformat(previous["source_time"]):
                return
        db.execute(
            "INSERT OR REPLACE INTO current_state "
            "(entity_id,source_time,data,snapshot_started_at) VALUES(?,?,?,?)",
            (entity_id, instant.isoformat(), data.model_dump_json(exclude_unset=True), boundary),
        )

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
        reason: str,
        entity_id: str | None = None,
    ) -> GapRecord:
        scope = entity_id or "*"
        existing = db.execute(
            "SELECT data FROM coverage WHERE scope=? AND recovered_at IS NULL", (scope,)
        ).fetchone()
        if existing:
            return gap_record.validate_json(existing[0])
        data: GapRecord = {
            "id": str(uuid4()),
            "source_id": source_id,
            "entity_ids": [entity_id] if entity_id else cls.selection(db)["entity_ids"],
            "last_confirmed_at": cls.last_confirmed(db, entity_id),
            "uncertain_start": True,
            "detected_at": detected_at,
            "recovered_at": None,
            "reason": reason,
        }
        db.execute(
            "INSERT INTO coverage VALUES(?, ?, ?, NULL, ?)",
            (data["id"], scope, detected_at, json.dumps(data)),
        )
        return data

    @staticmethod
    def open_gaps(db: sqlite3.Connection, entity_id: str | None = None) -> list[GapRecord]:
        if entity_id is None:
            rows = db.execute("SELECT data FROM coverage WHERE recovered_at IS NULL")
        else:
            rows = db.execute(
                "SELECT data FROM coverage WHERE recovered_at IS NULL AND scope IN ('*', ?)",
                (entity_id,),
            )
        return [gap_record.validate_json(row[0]) for row in rows]
