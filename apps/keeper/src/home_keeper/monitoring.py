"""The selection: which devices the user monitors, and what Home Assistant reports for each.

Selecting a device checks Home Assistant's catalog through the reader.
"""

from dataclasses import dataclass

from home_keeper.models import (
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


class Monitoring:
    def __init__(self, store: Store, reader: Reader) -> None:
        self.store = store
        self.reader = reader

    def selection(self) -> Selection:
        with self.store.transaction("DEFERRED") as db:
            return Selection(entity_ids=self.store.selected(db))

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
            if selected:
                db.execute(
                    "INSERT OR IGNORE INTO selection(id, device_kind) VALUES(?, ?)",
                    (entity_id, kind),
                )
            else:
                db.execute("DELETE FROM selection WHERE id=?", (entity_id,))
            return Selection(entity_ids=store.selected(db))

    def kinds(self) -> dict[str, DeviceKind | None]:
        """Each monitored device with the kind it was selected as, in ID order."""
        with self.store.transaction("DEFERRED") as db:
            rows = db.execute("SELECT id, device_kind FROM selection ORDER BY id").fetchall()
        return {row[0]: stored_kind.validate_python(row[1]) for row in rows}

    def devices(self) -> MonitoredEntities:
        """Every monitored device with what Home Assistant reports for it now. While Home
        Assistant cannot be read, each device is listed by its ID with no state."""
        kinds = self.kinds()
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
