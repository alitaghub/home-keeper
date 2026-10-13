import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class Store:
    def __init__(self, path: Path):
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

    @contextmanager
    def transaction(self):
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
    def selection(db):
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
    def entity(db, entity_id):
        row = db.execute("SELECT data FROM catalog WHERE id=?", (entity_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def cleanup(db, cutoff):
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
        db.execute(
            "DELETE FROM baselines WHERE json_extract(data, '$.persisted_at') < ?", (cutoff,)
        )

    @staticmethod
    def update_current(db, entity_id, source_time, data):
        previous = db.execute(
            "SELECT source_time FROM current_state WHERE entity_id=?", (entity_id,)
        ).fetchone()
        # Legacy JSON uses Z, while wire ingestion uses +00:00. Compare instants.
        instant = datetime.fromisoformat(source_time).astimezone(UTC)
        if previous is None or instant >= datetime.fromisoformat(previous[0]):
            db.execute(
                "INSERT OR REPLACE INTO current_state VALUES(?, ?, ?)",
                (entity_id, instant.isoformat(), json.dumps(data)),
            )

    @staticmethod
    def last_confirmed(db, entity_id=None):
        if entity_id is not None:
            row = db.execute(
                "SELECT persisted_at FROM confirmed WHERE entity_id=?", (entity_id,)
            ).fetchone()
            return row[0] if row else None
        row = db.execute("SELECT MAX(persisted_at) FROM confirmed").fetchone()
        retained = db.execute("SELECT value FROM metadata WHERE key='last_healthy_at'").fetchone()
        health = db.execute("SELECT value FROM metadata WHERE key='health'").fetchone()
        timestamps = [row[0]] if row[0] else []
        if retained:
            timestamps.append(retained[0])
        if health:
            value = json.loads(health[0])
            if value["status"] == "healthy":
                timestamps.append(value["persisted_at"])
        return max(timestamps) if timestamps else None

    @classmethod
    def interrupt(cls, db, source_id, detected_at, reason, entity_id=None):
        from uuid import uuid4

        scope = entity_id or "*"
        existing = db.execute(
            "SELECT data FROM coverage WHERE scope=? AND recovered_at IS NULL", (scope,)
        ).fetchone()
        if existing:
            return json.loads(existing[0])
        data = dict(
            id=str(uuid4()),
            source_id=source_id,
            entity_ids=[entity_id] if entity_id else cls.selection(db)["entity_ids"],
            last_confirmed_at=cls.last_confirmed(db, entity_id),
            uncertain_start=True,
            detected_at=detected_at,
            recovered_at=None,
            reason=reason,
        )
        db.execute(
            "INSERT INTO coverage VALUES(?, ?, ?, NULL, ?)",
            (data["id"], scope, detected_at, json.dumps(data)),
        )
        return data

    @staticmethod
    def open_gaps(db, entity_id=None):
        if entity_id is None:
            rows = db.execute("SELECT data FROM coverage WHERE recovered_at IS NULL")
        else:
            rows = db.execute(
                "SELECT data FROM coverage WHERE recovered_at IS NULL AND scope IN ('*', ?)",
                (entity_id,),
            )
        return [json.loads(row[0]) for row in rows]
