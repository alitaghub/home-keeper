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
