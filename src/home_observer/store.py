import json
import sqlite3
from contextlib import contextmanager
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
