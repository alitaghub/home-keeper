import sqlite3
from pathlib import Path

from home_observer.store import Store


def test_usage_read_transaction_does_not_hold_up_collector_writes(tmp_path: Path) -> None:
    store = Store(tmp_path / "lock.sqlite")
    # The same read transaction /api/usage opens while it copies rows.
    with store.transaction("DEFERRED") as reader:
        reader.execute("SELECT id FROM selection").fetchall()
        reader.execute("SELECT entity_id, data FROM baselines").fetchall()
        writer = sqlite3.connect(store.path, timeout=0.5)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO selection VALUES('light.kitchen')")
            writer.commit()  # a collector write committing mid-report
        finally:
            writer.close()
