"""Changes: what History lists, derived from Home Assistant's recorded rows.

Home Assistant records a row for each state it writes. A change is two consecutive rows of
one device whose state, availability, brightness or color differ. Pages run newest first;
the cursor carries the window, so later pages stay in the window of the first.
"""

import base64
from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise
from typing import Self

from pydantic import AwareDatetime, model_validator

from home_keeper.models import ChangeRecord, EventsPage, Identity, Reading, WireModel, to_utc
from home_keeper.reader import HistoryRow

# Fields that do not tell one state from another.
_SOURCE_TIMES = frozenset({"last_changed", "last_updated", "last_reported"})


class EventsCursor(WireModel):
    """Where the next page starts: the window of the first page and the last change shown."""

    start: AwareDatetime
    end: AwareDatetime
    time: AwareDatetime
    entity_id: Identity

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.start > self.end:
            raise ValueError("Reversed window")
        return self


def changes(
    entity_id: Identity, rows: Sequence[HistoryRow], start: datetime, end: datetime
) -> list[ChangeRecord]:
    """The changes of one device whose time is in `[start, end)`, oldest first. The first row
    is the state before the window, never a change by itself."""
    found: list[ChangeRecord] = []
    for old, new in pairwise(rows):
        if _meaning(old.reading) == _meaning(new.reading):
            continue
        time = new.updated_at
        if not start <= time < end:
            continue
        found.append(
            ChangeRecord(
                id=f"{entity_id}@{time.isoformat()}",
                entity_id=entity_id,
                source_entity_id=new.source_entity_id,
                device_kind=new.reading.device_kind,
                time=time.isoformat(),
                old=old.reading,
                new=new.reading,
            )
        )
    return found


def events_page(
    found: Sequence[ChangeRecord],
    *,
    start: datetime,
    end: datetime,
    after: EventsCursor | None,
    limit: int,
) -> EventsPage:
    """Up to `limit` changes after the cursor, newest first, ties by entity ID descending."""
    ordered = sorted(found, key=_position, reverse=True)
    if after is not None:
        last = (to_utc(after.time), after.entity_id)
        ordered = [change for change in ordered if _position(change) < last]
    shown = ordered[:limit]
    next_cursor = None
    if len(ordered) > limit:
        time, entity_id = _position(shown[-1])
        next_cursor = write_cursor(
            EventsCursor(start=start, end=end, time=time, entity_id=entity_id)
        )
    return EventsPage(events=shown, next_cursor=next_cursor)


def write_cursor(cursor: EventsCursor) -> str:
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode("ascii")


def read_cursor(cursor: str) -> EventsCursor:
    """Raises ValueError for a cursor this module did not write."""
    # binascii.Error, UnicodeError and ValidationError are all ValueErrors.
    return EventsCursor.model_validate_json(base64.urlsafe_b64decode(cursor.encode("ascii")))


def _meaning(reading: Reading) -> dict[str, object]:
    return reading.model_dump(exclude=set(_SOURCE_TIMES), exclude_none=True)


def _position(change: ChangeRecord) -> tuple[datetime, str]:
    return to_utc(datetime.fromisoformat(change.time)), change.entity_id
