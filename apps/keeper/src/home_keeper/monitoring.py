"""The selection: which devices the user monitors, and what Home Assistant reports for each.

Selecting a device checks Home Assistant's catalog through the reader. A monitored device's
kind follows the catalog; the stored kind is only for when Home Assistant cannot be read.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from home_keeper.models import (
    MAX_SELECTION,
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
class SelectionFull:
    """Already MAX_SELECTION devices are monitored."""


class Monitoring:
    def __init__(self, store: Store, reader: Reader) -> None:
        self.store = store
        self.reader = reader

    def selection(self) -> Selection:
        with self.store.transaction() as db:
            return Selection(entity_ids=self.store.selected(db))

    def select(
        self, entity_id: str, *, selected: bool
    ) -> Selection | UnknownEntity | UnsupportedEntity | SelectionFull:
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
                # Held to the commit, so two selections at once cannot both take the last place.
                db.execute("LOCK TABLE selection IN SHARE ROW EXCLUSIVE MODE")
                current = store.selected(db)
                if entity_id not in current and len(current) >= MAX_SELECTION:
                    return SelectionFull()
                db.execute(
                    "INSERT INTO selection(id, device_kind) VALUES(%s, %s) ON CONFLICT DO NOTHING",
                    (entity_id, kind),
                )
            else:
                db.execute("DELETE FROM selection WHERE id=%s", (entity_id,))
            return Selection(entity_ids=store.selected(db))

    def kinds(self) -> dict[str, DeviceKind | None]:
        """Each monitored device with its kind in Home Assistant's catalog, in ID order. While
        Home Assistant cannot be read, or no longer lists a device, its stored kind."""
        stored = self._stored_kinds()
        try:
            catalog = {entity.id: entity for entity in self.reader.catalog()}
        except ReaderUnavailable:
            return stored
        return self._follow(stored, catalog)

    def _stored_kinds(self) -> dict[str, DeviceKind | None]:
        with self.store.transaction() as db:
            rows = db.execute("SELECT id, device_kind FROM selection ORDER BY id").fetchall()
        return {row[0]: stored_kind.validate_python(row[1]) for row in rows}

    def _follow(
        self, stored: Mapping[str, DeviceKind | None], catalog: Mapping[str, Entity]
    ) -> dict[str, DeviceKind | None]:
        """The catalog's kind for each device it lists with one, stored for later."""
        kinds = dict(stored)
        for entity_id in kinds:
            entity = catalog.get(entity_id)
            if entity is not None and entity.device_kind is not None:
                kinds[entity_id] = entity.device_kind
        changed = [
            (kind, entity_id) for entity_id, kind in kinds.items() if kind != stored[entity_id]
        ]
        if changed:
            with self.store.transaction() as db:
                # A device stopped meanwhile stays stopped: an UPDATE adds no row.
                db.cursor().executemany("UPDATE selection SET device_kind=%s WHERE id=%s", changed)
        return kinds

    def devices(self) -> MonitoredEntities:
        """Every monitored device with what Home Assistant reports for it now. While Home
        Assistant cannot be read, each device is listed by its ID with no state."""
        kinds = self._stored_kinds()
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
        for entity_id, kind in self._follow(kinds, catalog).items():
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
