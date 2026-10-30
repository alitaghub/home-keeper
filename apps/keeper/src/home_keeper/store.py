import logging
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from home_keeper.models import CommandRecord, DeviceKind

# Marks a database written in the current shape. Any other file is an earlier release's copy
# of device history, which Home Assistant now serves, so it is replaced.
SCHEMA = "ha-history-1"
# How long a connection waits for another one's lock.
BUSY_SECONDS = 10.0
# The first bytes of every SQLite database file.
_SQLITE_HEADER = b"SQLite format 3\x00"

stored_kind: TypeAdapter[DeviceKind | None] = TypeAdapter(DeviceKind | None)

logger = logging.getLogger(__name__)


def stored_json(record: CommandRecord) -> str:
    """A record's row: the fields it was given, so absent attributes stay absent."""
    return record.model_dump_json(exclude_unset=True)


def _old(path: Path) -> bool:
    """Whether the file is surely not a current database: not SQLite, or SQLite without the
    current schema marker. A locked, unreadable or damaged database raises instead."""
    with path.open("rb") as file:
        header = file.read(len(_SQLITE_HEADER))
    if not header:
        # An empty file holds nothing; SQLite starts a new database in it.
        return False
    if header != _SQLITE_HEADER:
        return True
    try:
        with closing(sqlite3.connect(path, timeout=BUSY_SECONDS)) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
    except sqlite3.OperationalError as error:
        if str(error).startswith("no such table"):
            return True
        raise
    return row is None or row[0] != SCHEMA


def _replace_old(path: Path) -> None:
    """Delete a database an earlier release wrote, with its WAL files, so a new one starts."""
    if not path.exists() or not _old(path):
        return
    for old in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
        old.unlink(missing_ok=True)
    logger.warning(
        "Removed the old Home Keeper database: Home Assistant now keeps device history, "
        "so select your devices again"
    )


class Store:
    """The monitored devices, the command log and a few metadata values. Device history is
    Home Assistant's."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        _replace_old(path)
        with closing(sqlite3.connect(path, timeout=BUSY_SECONDS)) as db:
            # The mode is stored in the database file, so setting it once at startup is enough.
            db.execute("PRAGMA journal_mode=WAL")
        with self.transaction() as db:
            db.executescript("""
                -- The kind a device was selected as, for when Home Assistant cannot be read.
                CREATE TABLE IF NOT EXISTS selection(id TEXT PRIMARY KEY, device_kind TEXT);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS commands(
                    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, sent_at TEXT NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS command_order ON commands(sent_at, id);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES('schema', ?)", (SCHEMA,))

    @contextmanager
    def transaction(
        self, mode: Literal["IMMEDIATE", "DEFERRED"] = "IMMEDIATE"
    ) -> Generator[sqlite3.Connection]:
        """IMMEDIATE takes the write lock up front; DEFERRED suits reads that need no lock."""
        db = sqlite3.connect(self.path, timeout=BUSY_SECONDS)
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
    def selected(db: sqlite3.Connection) -> list[str]:
        """The monitored devices' IDs, in order."""
        return [row[0] for row in db.execute("SELECT id FROM selection ORDER BY id")]

    @staticmethod
    def cleanup(db: sqlite3.Connection, cutoff: str) -> None:
        """Forget commands sent before `cutoff`. The API stamps sent_at with its own clock."""
        db.execute("DELETE FROM commands WHERE sent_at < ?", (cutoff,))
