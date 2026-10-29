"""A reader over what the collector ingest routes recorded, for tests written against them.

The API reads Home Assistant through a reader; these tests still seed it through
`/internal/catalog` and `/internal/baselines`. This reader serves those rows back.
"""

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from pydantic import TypeAdapter

from home_keeper.models import Entity, Identity, LightReading, Reading, SwitchReading
from home_keeper.reader import LiveState
from home_keeper.store import current_reading

_reading: TypeAdapter[Reading] = TypeAdapter(Reading)


class RecordedReader:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _rows(self, query: str, parameters: tuple[str, ...] = ()) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.db_path)) as db:
            try:
                return db.execute(query, parameters).fetchall()
            except sqlite3.OperationalError:
                # The app has not created its tables yet.
                return []

    def health(self) -> None:
        return None

    def catalog(self) -> list[Entity]:
        return [
            Entity.model_validate_json(data)
            for _, data in self._rows("SELECT id, data FROM catalog ORDER BY id")
        ]

    def states(self, entity_ids: Sequence[Identity]) -> dict[Identity, LiveState | None]:
        result: dict[Identity, LiveState | None] = {}
        for entity_id in entity_ids:
            rows = self._rows(
                "SELECT entity_id, data FROM current_state WHERE entity_id=?", (entity_id,)
            )
            current = current_reading.validate_json(rows[0][1]) if rows else None
            if current is None:
                result[entity_id] = None
                continue
            fields = (
                LightReading.model_fields
                if current.device_kind == "light"
                else SwitchReading.model_fields
            )
            result[entity_id] = LiveState(
                # Test readings often name no source; the identity stands in for it.
                source_entity_id=current.source_entity_id or entity_id,
                reading=_reading.validate_python(
                    current.model_dump(include=set(fields), exclude_none=True)
                    | {"device_kind": current.device_kind}
                ),
            )
        return result
