"""The selection: which devices the user monitors, and what Home Keeper knows of each one.

Selecting a device bumps the selection revision and makes it wait for a new confirmation.
`device` and `admits` read inside the caller's transaction, so a recorder or a command can
check a device and act on it with no select slipping in between.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from home_keeper.models import (
    CurrentReading,
    DeviceKind,
    Entity,
    Freshness,
    MonitoredEntities,
    MonitoredEntity,
    SelectionSnapshot,
)
from home_keeper.store import Store, stored_kind


@dataclass(frozen=True)
class UnknownEntity:
    pass


@dataclass(frozen=True)
class UnsupportedEntity:
    pass


@dataclass(frozen=True)
class Device:
    """What Home Keeper knows of one device, monitored or not."""

    entity_id: str
    # None once the device has left discovery.
    entity: Entity | None
    monitored: bool
    kind: DeviceKind | None
    reading: CurrentReading | None


class Monitoring:
    def __init__(
        self, store: Store, *, now: Callable[[], datetime], reconciliation_seconds: int
    ) -> None:
        """`reconciliation_seconds`: a monitored device whose newest confirmation is older
        than two of these is stale."""
        self.store = store
        self.now = now
        self.reconciliation_seconds = reconciliation_seconds

    def selection(self) -> SelectionSnapshot:
        with self.store.transaction() as db:
            return self.store.selection(db)

    def select(
        self, entity_id: str, *, selected: bool
    ) -> SelectionSnapshot | UnknownEntity | UnsupportedEntity:
        """Start or stop monitoring a device. A device that left discovery can still be
        stopped."""
        store = self.store
        with store.transaction() as db:
            entity = store.entity(db, entity_id)
            current = store.selection(db)
            if entity is None and (selected or entity_id not in current.entity_ids):
                return UnknownEntity()
            if selected and entity is not None and not entity.supported:
                return UnsupportedEntity()
            if selected != (entity_id in current.entity_ids):
                if selected:
                    db.execute(
                        "INSERT INTO selection(id, device_kind) VALUES(?, ?)",
                        (entity_id, entity.device_kind if entity else None),
                    )
                    # A fresh selection awaits a new confirmation, even if history exists.
                    db.execute("DELETE FROM confirmed WHERE entity_id=?", (entity_id,))
                    db.execute("DELETE FROM current_state WHERE entity_id=?", (entity_id,))
                else:
                    db.execute("DELETE FROM selection WHERE id=?", (entity_id,))
                db.execute(
                    "UPDATE metadata SET value=? WHERE key='revision'",
                    (str(current.revision + 1),),
                )
            return store.selection(db)

    def devices(self, *, collector_healthy: bool) -> MonitoredEntities:
        """Every monitored device with its newest confirmed reading and its freshness. A
        device is fresh only while the collector is healthy."""
        store = self.store
        results: list[MonitoredEntity] = []
        with store.transaction("DEFERRED") as db:
            for entity_id in store.selection(db).entity_ids:
                device = self.device(db, entity_id)
                entity = device.entity or Entity(
                    id=entity_id,
                    name=entity_id,
                    device_id=None,
                    device_name=None,
                    device_kind=device.kind,
                    supported=False,
                    reason="Entity missing from latest discovery",
                )
                confirmed = db.execute(
                    "SELECT * FROM confirmed WHERE entity_id=?", (entity_id,)
                ).fetchone()
                freshness: Freshness = "pending"
                if confirmed:
                    fresh = (
                        self._recent(confirmed["received_at"])
                        and self._recent(confirmed["persisted_at"])
                        and collector_healthy
                        and not store.open_gaps(db, entity_id)
                        and device.entity is not None
                        and entity.supported
                    )
                    freshness = "fresh" if fresh else "stale"
                results.append(
                    MonitoredEntity(
                        **entity.model_dump(),
                        baseline=device.reading if confirmed else None,
                        freshness=freshness,
                        confirmed_at=confirmed["received_at"] if confirmed else None,
                    )
                )
        return MonitoredEntities(entities=results)

    @staticmethod
    def device(db: sqlite3.Connection, entity_id: str) -> Device:
        entity = Store.entity(db, entity_id)
        reading = Store.current(db, entity_id)
        selected = db.execute(
            "SELECT device_kind FROM selection WHERE id=?", (entity_id,)
        ).fetchone()
        # Discovery names the kind of every supported device; a catalog entry without one
        # falls back to the newest reading, then to the kind the device was selected as.
        kind = (
            (entity.device_kind if entity else None)
            or (reading.device_kind if reading else None)
            or (stored_kind.validate_python(selected[0]) if selected else None)
        )
        return Device(
            entity_id=entity_id,
            entity=entity,
            monitored=selected is not None,
            kind=kind,
            reading=reading,
        )

    @staticmethod
    def admits(db: sqlite3.Connection, entity_id: str, revision: int) -> bool:
        """Whether a reading taken under selection `revision` may still be recorded."""
        selection = Store.selection(db)
        entity = Store.entity(db, entity_id)
        return (
            revision == selection.revision
            and entity_id in selection.entity_ids
            and entity is not None
            and entity.supported
        )

    def _recent(self, timestamp: str) -> bool:
        age = (self.now() - datetime.fromisoformat(timestamp)).total_seconds()
        return 0 <= age <= 2 * self.reconciliation_seconds
