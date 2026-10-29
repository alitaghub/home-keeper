"""A stand-in for the reader service: the catalog and the live states the API reads."""

from collections.abc import Callable, Sequence

from pydantic import TypeAdapter

from home_keeper.models import Entity, Reading
from home_keeper.reader import LiveState, ReaderError, ReaderUnavailable

type Payload = dict[str, object]

reading_of: TypeAdapter[Reading] = TypeAdapter(Reading)
CHANGED_AT = "2026-10-12T09:00:00+00:00"


def live(source_entity_id: str, state: Payload) -> LiveState:
    """What Home Assistant reports for `source_entity_id`: a light's state, or a switch's
    when `state` names `device_kind` "switch". Availability follows the state."""
    value = state["state"]
    return LiveState(
        source_entity_id=source_entity_id,
        reading=reading_of.validate_python(
            {
                "device_kind": "light",
                "availability": "available" if value in ("on", "off") else value,
                "last_changed": CHANGED_AT,
                "last_updated": CHANGED_AT,
            }
            | state
        ),
    )


class RunawayPollingError(BaseException):
    """Raised from a read when a command keeps polling far past any sane deadline.

    A BaseException, so the command cannot treat it as a failed read and poll on.
    """


class FakeReader:
    """Answers like the reader service, from `entities` and `states`.

    Set `error` to make every call raise `ReaderUnavailable(error)`, as when the reader or
    Home Assistant cannot be reached. `on_read` runs at the start of each `states` call,
    before the answer is taken, so a test can change the home or the clock between reads.
    """

    def __init__(
        self, entities: Sequence[Entity] = (), states: dict[str, LiveState] | None = None
    ) -> None:
        self.entities = list(entities)
        self.live: dict[str, LiveState] = dict(states or {})
        self.error: ReaderError | None = None
        self.on_read: Callable[[], object] | None = None
        # The entity ids of each `states` call, in order.
        self.reads: list[list[str]] = []
        self.max_reads = 500

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
        return {entity_id: self.live.get(entity_id) for entity_id in entity_ids}

    def set(self, entity_id: str, source_entity_id: str, state: Payload) -> None:
        """Home Assistant now reports `state` for the device."""
        self.live[entity_id] = live(source_entity_id, state)

    def drop(self, entity_id: str) -> None:
        """Home Assistant no longer has the device: gone from the registry and the states."""
        self.entities = [entity for entity in self.entities if entity.id != entity_id]
        self.live.pop(entity_id, None)

    def _check(self) -> None:
        if self.error is not None:
            raise ReaderUnavailable(self.error)
