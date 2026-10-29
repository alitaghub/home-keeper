"""The selection: which devices the user monitors, and what Home Assistant reports for each.

Selecting a device checks Home Assistant's catalog through the reader. `device` and
`admits` serve the collector ingest routes: they read inside the caller's transaction, so a
recorder can check a device and act on it with no select slipping in between.
"""

import sqlite3
from dataclasses import dataclass

from home_keeper.models import (
    CurrentReading,
    DeviceKind,
    Entity,
    MonitoredEntities,
    MonitoredEntity,
    Selection,
)
from home_keeper.reader import Reader, ReaderUnavailable
from home_keeper.store import Store, stored_kind

MISSING_REASON = "Missing from Home Assistant"


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
    def __init__(self, store: Store, reader: Reader) -> None:
        self.store = store
        self.reader = reader

    def selection(self) -> Selection:
        with self.store.transaction("DEFERRED") as db:
            return Selection(entity_ids=self.store.selection(db).entity_ids)

    def select(
        self, entity_id: str, *, selected: bool
    ) -> Selection | UnknownEntity | UnsupportedEntity:
        """Start or stop monitoring a device. Starting checks Home Assistant's catalog and
        raises ReaderUnavailable when it cannot be read; stopping needs no reader, so a
        device that left Home Assistant can still be stopped."""
        kind: DeviceKind | None = None
        if selected:
            entity = next((e for e in self.reader.catalog() if e.id == entity_id), None)
            if entity is None:
                return UnknownEntity()
            if not entity.supported:
                return UnsupportedEntity()
            kind = entity.device_kind
        store = self.store
        with store.transaction() as db:
            current = store.selection(db)
            if selected != (entity_id in current.entity_ids):
                if selected:
                    db.execute(
                        "INSERT INTO selection(id, device_kind) VALUES(?, ?)", (entity_id, kind)
                    )
                    # The collector ingest routes still record into these until they go.
                    db.execute("DELETE FROM confirmed WHERE entity_id=?", (entity_id,))
                    db.execute("DELETE FROM current_state WHERE entity_id=?", (entity_id,))
                else:
                    db.execute("DELETE FROM selection WHERE id=?", (entity_id,))
                db.execute(
                    "UPDATE metadata SET value=? WHERE key='revision'",
                    (str(current.revision + 1),),
                )
            return Selection(entity_ids=store.selection(db).entity_ids)

    def devices(self) -> MonitoredEntities:
        """Every monitored device with what Home Assistant reports for it now. While Home
        Assistant cannot be read, each device is listed by its ID with no state."""
        with self.store.transaction("DEFERRED") as db:
            rows = db.execute("SELECT id, device_kind FROM selection ORDER BY id").fetchall()
        kinds: dict[str, DeviceKind | None] = {
            row[0]: stored_kind.validate_python(row[1]) for row in rows
        }
        try:
            catalog = {entity.id: entity for entity in self.reader.catalog()}
            states = self.reader.states(list(kinds))
        except ReaderUnavailable:
            return MonitoredEntities(
                entities=[
                    MonitoredEntity(
                        id=entity_id,
                        name=entity_id,
                        device_id=None,
                        device_name=None,
                        device_kind=kind,
                        supported=True,
                        reason=None,
                        source_entity_id=None,
                        state=None,
                        missing=False,
                    )
                    for entity_id, kind in kinds.items()
                ]
            )
        results: list[MonitoredEntity] = []
        for entity_id, kind in kinds.items():
            entity = catalog.get(entity_id)
            live = states.get(entity_id)
            described = entity or Entity(
                id=entity_id,
                name=entity_id,
                device_id=None,
                device_name=None,
                device_kind=kind,
                supported=False,
                reason=MISSING_REASON,
            )
            results.append(
                MonitoredEntity(
                    **described.model_dump(),
                    source_entity_id=live.source_entity_id if live else None,
                    state=live.reading if live else None,
                    missing=entity is None or live is None,
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
