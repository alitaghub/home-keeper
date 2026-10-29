"""The API's view of Home Assistant: the reader service, reached over internal HTTP. The
API asks it for the catalog, current states, history and reachability on each request."""

from collections.abc import Sequence
from datetime import datetime
from http import HTTPStatus
from typing import Literal, Protocol, Self

import httpx
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator

from home_keeper.models import (
    CollectorError,
    Entity,
    Identity,
    ReaderError,
    Reading,
    WireModel,
    to_utc,
)

__all__ = [
    "HistoryRow",
    "HttpReader",
    "LiveState",
    "Reader",
    "ReaderError",
    "ReaderUnavailable",
]

# Above the reader's own 5-second Home Assistant timeout, so a slow answer is not read
# as an unreachable reader. Discovery makes several Home Assistant calls.
READER_TIMEOUT_SECONDS = 10


class ReaderUnavailable(Exception):  # noqa: N818 -- names the condition, like ControllerUnreachable
    """Home Assistant could not be read. Carries a fixed safe code, never upstream text."""

    def __init__(self, error: ReaderError) -> None:
        self.error: ReaderError = error
        super().__init__(f"Home Assistant cannot be read ({error})")


class LiveState(WireModel):
    """What Home Assistant reports for one entity right now."""

    source_entity_id: Identity
    reading: Reading


class HistoryRow(WireModel):
    """One row of Home Assistant's recorded history for one entity."""

    source_entity_id: Identity
    reading: Reading
    _updated_at: datetime = PrivateAttr()

    @model_validator(mode="after")
    def source_times(self) -> Self:
        """Home Assistant's history has both times; each must be a timezone-aware time."""
        times = (self.reading.last_changed, self.reading.last_updated)
        changed, updated = (None if value is None else _aware(value) for value in times)
        if changed is None or updated is None:
            raise ValueError("A history row needs its source times")
        self._updated_at = updated
        return self

    @property
    def updated_at(self) -> datetime:
        """When Home Assistant last updated the state: the time of a change to this row."""
        return self._updated_at


def _aware(value: str) -> datetime:
    """Raises ValueError for a malformed time or one without a timezone."""
    return to_utc(datetime.fromisoformat(value))


class Reader(Protocol):
    def health(self) -> None:
        """Return when Home Assistant answers. Raises ReaderUnavailable."""
        ...

    def catalog(self) -> list[Entity]:
        """Home Assistant's devices as Home Keeper projects them. Raises ReaderUnavailable."""
        ...

    def states(self, entity_ids: Sequence[Identity]) -> dict[Identity, LiveState | None]:
        """One key per requested ID; None when Home Assistant no longer has the entity.
        Raises ReaderUnavailable."""
        ...

    def history(
        self, entity_ids: Sequence[Identity], start: datetime, end: datetime
    ) -> dict[Identity, list[HistoryRow]]:
        """One key per requested ID, rows oldest first. The first row is the state at
        `start`. [] when Home Assistant has no history for the entity or no longer has it.
        Raises ReaderUnavailable."""
        ...


# The reader service's internal bodies, shared by the service and HttpReader.
class ReaderHealthy(WireModel):
    status: Literal["healthy"]


class ReaderFailure(WireModel):
    """The 503 body of every reader route."""

    error: CollectorError


class ReaderCatalog(WireModel):
    entities: list[Entity] = Field(max_length=500)


class ReaderStates(WireModel):
    states: dict[Identity, LiveState | None]


class ReaderHistory(WireModel):
    history: dict[Identity, list[HistoryRow]]


class HttpReader:
    def __init__(self, url: str, service_token: str) -> None:
        self._http = httpx.Client(
            # A fixed internal origin from settings, not a user-supplied passthrough.
            base_url=url,
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=READER_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
        )

    def health(self) -> None:
        self._get("/internal/health", ReaderHealthy)

    def catalog(self) -> list[Entity]:
        return self._get("/internal/catalog", ReaderCatalog).entities

    def states(self, entity_ids: Sequence[Identity]) -> dict[Identity, LiveState | None]:
        if not entity_ids:
            return {}
        states = self._get(
            "/internal/states", ReaderStates, params={"entity_id": list(entity_ids)}
        ).states
        return {entity_id: states.get(entity_id) for entity_id in entity_ids}

    def history(
        self, entity_ids: Sequence[Identity], start: datetime, end: datetime
    ) -> dict[Identity, list[HistoryRow]]:
        if not entity_ids:
            return {}
        history = self._get(
            "/internal/history",
            ReaderHistory,
            params={
                "entity_id": list(entity_ids),
                "start": [start.isoformat()],
                "end": [end.isoformat()],
            },
        ).history
        return {entity_id: history.get(entity_id, []) for entity_id in entity_ids}

    def _get[T: BaseModel](
        self, path: str, model: type[T], params: dict[str, list[str]] | None = None
    ) -> T:
        try:
            response = self._http.get(path, params=params)
        except httpx.HTTPError:
            raise ReaderUnavailable("reader_unreachable") from None
        if response.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            try:
                failure = ReaderFailure.model_validate_json(response.content)
            except ValidationError:
                raise ReaderUnavailable("reader_unreachable") from None
            raise ReaderUnavailable(failure.error)
        if response.status_code != HTTPStatus.OK:
            raise ReaderUnavailable("reader_unreachable")
        try:
            return model.model_validate_json(response.content)
        except ValidationError:
            raise ReaderUnavailable("invalid_data") from None
