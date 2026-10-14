from pathlib import Path

from home_observer.store import Store


def test_global_event_page_does_not_need_a_full_table_sort(tmp_path: Path) -> None:
    store = Store(tmp_path / "plan.sqlite")
    with store.transaction() as db:
        plan = [
            row[3]
            for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT data FROM events "
                "ORDER BY event_time DESC, id DESC LIMIT 51"
            )
        ]
    assert not any("TEMP B-TREE" in detail for detail in plan), plan


def test_event_expiry_uses_an_index(tmp_path: Path) -> None:
    store = Store(tmp_path / "expiry.sqlite")
    with store.transaction() as db:
        plan = [
            row[3]
            for row in db.execute(
                "EXPLAIN QUERY PLAN DELETE FROM events WHERE persisted_at < ?",
                ("2026-01-01T00:00:00+00:00",),
            )
        ]
    assert any("INDEX" in detail for detail in plan), plan
