"""A stand-in for the reader service: the catalog, the live states and the history the API
reads."""

from collections.abc import Callable, Sequence
from datetime import datetime

from pydantic import TypeAdapter

from home_keeper.models import Entity, Reading
from home_keeper.reader import (
    RAW_WORDS,
    HistoryRow,
    LiveState,
    OnOffWords,
    ReaderError,
    ReaderUnavailable,
)

type Payload = dict[str, object]

reading_of: TypeAdapter[Reading] = TypeAdapter(Reading)
CHANGED_AT = "2026-10-12T09:00:00+00:00"


def live(source_entity_id: str, state: Payload) -> LiveState:
    """What Home Assistant reports for `source_entity_id`: a light's state, or a switch's or a
    generic device's when `state` names `device_kind` "switch" or "state". Availability follows
    the state. The label is `state`'s own `label`, else the state with a capital letter, as
    Home Assistant translates `on` to "On"."""
    value = state["state"]
    return LiveState(
        source_entity_id=source_entity_id,
        reading=reading_of.validate_python(
            {
                "device_kind": "light",
                "availability": value if value in ("unavailable", "unknown") else "available",
                "label": str(value).capitalize(),
                "last_changed": CHANGED_AT,
                "last_updated": CHANGED_AT,
            }
            | state
        ),
    )


def history_row(
    source_entity_id: str, at: datetime, state: Payload, last_changed: datetime | None = None
) -> HistoryRow:
    """One row of Home Assistant's recorder: what `live` reports, recorded at `at`.

    `last_changed` defaults to `at`; pass the earlier time when only attributes changed.
    """
    recorded = live(source_entity_id, state).reading.model_dump(exclude_unset=True) | {
        "last_changed": (last_changed or at).isoformat(),
        "last_updated": at.isoformat(),
    }
    return HistoryRow(
        source_entity_id=source_entity_id, reading=reading_of.validate_python(recorded)
    )


def recorded_at(row: HistoryRow) -> datetime:
    assert row.reading.last_updated is not None, "a history row always has its time"
    return datetime.fromisoformat(row.reading.last_updated)


def period(rows: Sequence[HistoryRow], start: datetime, end: datetime) -> list[HistoryRow]:
    """What Home Assistant's `history/history_during_period` returns for one entity: the
    state at `start` first, then each row recorded after `start` and before `end`."""
    ordered = sorted(rows, key=recorded_at)
    before = [row for row in ordered if recorded_at(row) <= start]
    return before[-1:] + [row for row in ordered if start < recorded_at(row) < end]


class RunawayPollingError(BaseException):
    """Raised from a read when a command keeps polling far past any sane deadline.

    A BaseException, so the command cannot treat it as a failed read and poll on.
    """


class FakeReader:
    """Answers like the reader service, from `entities` and `states`.

    Set `error` to make every call raise `ReaderUnavailable(error)`, as when the reader or
    Home Assistant cannot be reached. `on_read` runs at the start of each `states` call,
    before the answer is taken, so a test can change the home or the clock between reads.
    `recorded` holds Home Assistant's recorder rows by entity ID, in any order.
    """

    def __init__(
        self, entities: Sequence[Entity] = (), states: dict[str, LiveState] | None = None
    ) -> None:
        self.entities = list(entities)
        self.live: dict[str, LiveState] = dict(states or {})
        self.error: ReaderError | None = None
        # Set to fail only the live-state reads, while the catalog and history still answer.
        self.states_error: ReaderError | None = None
        self.on_read: Callable[[], object] | None = None
        # The entity ids of each `states` call, in order.
        self.reads: list[list[str]] = []
        self.max_reads = 500
        self.recorded: dict[str, list[HistoryRow]] = {}
        # The (entity IDs, start, end) of each `history` call, in order.
        self.history_reads: list[tuple[list[str], datetime, datetime]] = []
        # Home Assistant's words for on and off by entity ID; a device not listed has none.
        self.word_list: dict[str, OnOffWords] = {}
        # Set to fail only the words reads, while the catalog, states and history still answer.
        self.words_error: ReaderError | None = None
        # The entity IDs of each `words` call, in order.
        self.word_reads: list[list[str]] = []

    def health(self) -> None:
        self._check()

    def catalog(self) -> list[Entity]:
        self._check()
        return list(self.entities)

    def states(self, entity_ids: Sequence[str]) -> dict[str, LiveState | None]:
        self.reads.append(list(entity_ids))
        if len(self.reads) > self.max_reads:
            raise RunawayPollingError(f"more than {self.max_reads} state reads")
        if self.on_read is not None:
            self.on_read()
        self._check()
        if self.states_error is not None:
            raise ReaderUnavailable(self.states_error)
        return {entity_id: self.live.get(entity_id) for entity_id in entity_ids}

    def history(
        self, entity_ids: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, list[HistoryRow]]:
        self.history_reads.append((list(entity_ids), start, end))
        self._check()
        return {
            entity_id: period(self.recorded.get(entity_id, []), start, end)
            for entity_id in entity_ids
        }

    def words(self, entity_ids: Sequence[str]) -> dict[str, OnOffWords]:
        self.word_reads.append(list(entity_ids))
        self._check()
        if self.words_error is not None:
            raise ReaderUnavailable(self.words_error)
        return {entity_id: self.word_list.get(entity_id, RAW_WORDS) for entity_id in entity_ids}

    def record(self, entity_id: str, *rows: HistoryRow) -> None:
        """Home Assistant's recorder stores `rows` for the device."""
        self.recorded.setdefault(entity_id, []).extend(rows)

    def set(self, entity_id: str, source_entity_id: str, state: Payload) -> None:
        """Home Assistant now reports `state` for the device."""
        self.live[entity_id] = live(source_entity_id, state)

    def drop(self, entity_id: str) -> None:
        """Home Assistant no longer has the device: gone from the registry and the states."""
        self.entities = [entity for entity in self.entities if entity.id != entity_id]
        self.live.pop(entity_id, None)
        self.recorded.pop(entity_id, None)

    def _check(self) -> None:
        if self.error is not None:
            raise ReaderUnavailable(self.error)
