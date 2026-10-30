from collections.abc import Generator
from contextlib import contextmanager

from psycopg import Connection
from psycopg_pool import ConnectionPool
from pydantic import TypeAdapter

from home_keeper.models import CommandRecord, DeviceKind

# The longest a statement, or its wait for a lock, may take before Postgres cancels it.
STATEMENT_TIMEOUT = "10s"

stored_kind: TypeAdapter[DeviceKind | None] = TypeAdapter(DeviceKind | None)

# Keys use the "C" collation, so ID order and the command cursor do not depend on the
# server's locale.
_SCHEMA = """
    -- The kind a device was selected as, for when Home Assistant cannot be read.
    CREATE TABLE IF NOT EXISTS selection(id TEXT COLLATE "C" PRIMARY KEY, device_kind TEXT);
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS commands(
        id TEXT COLLATE "C" PRIMARY KEY, entity_id TEXT NOT NULL,
        sent_at TIMESTAMPTZ NOT NULL, data JSONB NOT NULL
    );
    CREATE INDEX IF NOT EXISTS command_order ON commands(sent_at, id);
"""


def stored_json(record: CommandRecord) -> str:
    """A record's row: the fields it was given, so absent attributes stay absent."""
    return record.model_dump_json(exclude_unset=True)


class Store:
    """The monitored devices, the command log and a few metadata values, in Postgres. Device
    history is Home Assistant's."""

    def __init__(self, conninfo: str, *, password: str | None = None) -> None:
        """Connects nothing until `open`, so building the app needs no database. `password`
        joins `conninfo` when it is not None."""
        self._pool = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=8,
            open=False,
            check=ConnectionPool.check_connection,
            kwargs={
                "options": f"-c statement_timeout={STATEMENT_TIMEOUT} "
                f"-c lock_timeout={STATEMENT_TIMEOUT}",
            }
            | ({} if password is None else {"password": password}),
        )

    def open(self, timeout: float = 30.0) -> None:
        """Connect and create the tables. Raises PoolTimeout when Postgres does not answer."""
        self._pool.open(wait=True, timeout=timeout)
        with self.transaction() as db:
            db.execute(_SCHEMA)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def transaction(self) -> Generator[Connection]:
        """Commits when the block ends, and rolls back when it raises."""
        with self._pool.connection() as db:
            yield db

    @staticmethod
    def selected(db: Connection) -> list[str]:
        """The monitored devices' IDs, in order."""
        return [row[0] for row in db.execute("SELECT id FROM selection ORDER BY id")]

    @staticmethod
    def cleanup(db: Connection, cutoff: str) -> None:
        """Forget commands sent before `cutoff`. The API stamps sent_at with its own clock."""
        db.execute("DELETE FROM commands WHERE sent_at < %s", (cutoff,))
