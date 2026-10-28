"""Admits baselines and events, stores them, and decides each device's current state.

Every check and write for one reading runs in a single IMMEDIATE transaction, so no
other writer can change the selection or the current state between them.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NamedTuple
from uuid import uuid4

from pydantic import TypeAdapter

from home_keeper.models import (
    BaselineRecord,
    ChangeEvent,
    CurrentReading,
    DeviceObservation,
    DeviceState,
    EventReading,
    EventRecord,
    LightState,
    SwitchState,
)
from home_keeper.monitoring import Monitoring
from home_keeper.store import Store, baseline_record, current_reading, stored_json

device_state: TypeAdapter[DeviceState] = TypeAdapter(DeviceState)
event_reading: TypeAdapter[EventReading] = TypeAdapter(EventReading)


def state_of(reading: DeviceObservation | CurrentReading) -> LightState | SwitchState:
    """The device state a reading or observation records, without its record fields."""
    model = LightState if reading.device_kind == "light" else SwitchState
    return model.model_validate(reading.model_dump(include=set(model.model_fields)))


# Delivery context, not evidence: event IDs exclude these fields, so a re-sent event
# may differ in them. The stored record keeps the values it was first recorded with.
EVENT_CONTEXT_KEYS = frozenset({"received_at", "selection_revision"})


class SnapshotRead(NamedTuple):
    """When an authoritative HTTP state read started and when its result arrived."""

    started_at: datetime
    received_at: datetime


def event_outranks_snapshot(
    event_received_at: datetime, event_time: datetime, snapshot_time: datetime, read: SnapshotRead
) -> bool:
    """Whether an event captured during an HTTP read is newer than the read's sampled state."""
    return event_received_at >= read.started_at and event_time > snapshot_time


def snapshot_can_replace(
    db: sqlite3.Connection, entity_id: str, source_time: datetime, snapshot: SnapshotRead
) -> bool:
    previous = db.execute(
        "SELECT source_time, data FROM current_state WHERE entity_id=?", (entity_id,)
    ).fetchone()
    if previous is None:
        return True
    value = current_reading.validate_json(previous["data"])
    receipt = datetime.fromisoformat(value.received_at)
    if snapshot.received_at < receipt:
        return False
    return not (
        value.kind == "event"
        and event_outranks_snapshot(
            receipt, datetime.fromisoformat(previous["source_time"]), source_time, snapshot
        )
    )


def update_current(
    db: sqlite3.Connection,
    entity_id: str,
    source_time: str,
    data: CurrentReading,
    *,
    snapshot: SnapshotRead | None = None,
) -> None:
    previous = db.execute(
        "SELECT source_time, snapshot_started_at, snapshot_received_at "
        "FROM current_state WHERE entity_id=?",
        (entity_id,),
    ).fetchone()
    instant = datetime.fromisoformat(source_time).astimezone(UTC)
    boundary = previous["snapshot_started_at"] if previous else None
    snapshot_received_at = previous["snapshot_received_at"] if previous else None
    if snapshot is not None:
        if not snapshot_can_replace(db, entity_id, instant, snapshot):
            db.execute(
                "UPDATE current_state SET snapshot_started_at=?, snapshot_received_at=? "
                "WHERE entity_id=?",
                (snapshot.started_at.isoformat(), snapshot.received_at.isoformat(), entity_id),
            )
            return
        boundary = snapshot.started_at.isoformat()
        snapshot_received_at = snapshot.received_at.isoformat()
    else:
        # Keep delayed observations in history without crossing a newer HTTP read boundary.
        if (
            boundary
            and data.kind == "event"
            and datetime.fromisoformat(data.received_at) < datetime.fromisoformat(boundary)
        ):
            return
        if previous and instant < datetime.fromisoformat(previous["source_time"]):
            return
    db.execute(
        "INSERT OR REPLACE INTO current_state "
        "(entity_id,source_time,data,snapshot_started_at,snapshot_received_at) "
        "VALUES(?,?,?,?,?)",
        (
            entity_id,
            instant.isoformat(),
            stored_json(data),
            boundary,
            snapshot_received_at,
        ),
    )


type RejectionCode = Literal[
    "source_mismatch",
    "future_timestamp",
    "selection_changed",
    "observation_older",
    "event_identity_conflict",
]


@dataclass(frozen=True)
class Rejected:
    code: RejectionCode
    message: str


type EventOutcome = Literal["recorded", "duplicate", "unchanged"] | Rejected

SOURCE_MISMATCH = Rejected("source_mismatch", "Collection source mismatch")


class Recorder:
    def __init__(self, store: Store, source_id: str, now: Callable[[], datetime]) -> None:
        self.store = store
        self.source_id = source_id
        self.now = now

    def observe(self, payload: DeviceObservation) -> BaselineRecord | Rejected:
        """Record a state snapshot; returns the baseline it is stored as."""
        if payload.source_id != self.source_id:
            return SOURCE_MISMATCH
        persisted = self.now().astimezone(UTC)
        if payload.received_at > persisted:
            return Rejected("future_timestamp", "Future receipt timestamp")
        with self.store.transaction() as db:
            if not Monitoring.admits(db, payload.entity_id, payload.selection_revision):
                return Rejected("selection_changed", "Selection no longer permits this observation")
            # Only an earlier snapshot makes this one stale. An event received after the read
            # returned is newer evidence; current-state ordering keeps it ahead of this snapshot.
            previous_snapshot = db.execute(
                "SELECT snapshot_received_at FROM current_state WHERE entity_id=?",
                (payload.entity_id,),
            ).fetchone()
            if (
                previous_snapshot
                and previous_snapshot[0]
                and payload.received_at < datetime.fromisoformat(previous_snapshot[0])
            ):
                return Rejected("observation_older", "Observation is older than latest snapshot")
            confirmed = db.execute(
                "SELECT received_at FROM confirmed WHERE entity_id=?", (payload.entity_id,)
            ).fetchone()
            read = SnapshotRead(
                payload.collection_started_at or payload.received_at, payload.received_at
            )
            source_time = payload.last_updated or payload.received_at
            latest = self.store.current(db, payload.entity_id)
            # Only an authoritative snapshot lacking a recorded change exposes a gap.
            # A newer buffered event wins over an older reconciliation snapshot.
            if (
                confirmed
                and latest
                and state_of(payload).meaningful() != state_of(latest).meaningful()
                and snapshot_can_replace(db, payload.entity_id, source_time, read)
                and not self.store.open_gaps(db, payload.entity_id)
            ):
                self.store.interrupt(
                    db, self.source_id, persisted.isoformat(), "reconciliation", payload.entity_id
                )
            data = payload.model_dump(mode="json", exclude={"selection_revision"})
            gaps = self.store.open_gaps(db, payload.entity_id)
            data["kind"] = (
                "recovery" if confirmed and gaps else "reconciliation" if confirmed else "baseline"
            )
            if gaps:
                data["coverage_gap_ids"] = [gap.id for gap in gaps]
                data["transition_time"] = "unknown"
            baseline = self._store_baseline(db, payload.entity_id, data, persisted, bool(gaps))
            update_current(db, payload.entity_id, source_time.isoformat(), baseline, snapshot=read)
            if confirmed is None or payload.received_at >= datetime.fromisoformat(confirmed[0]):
                self._confirm(db, payload.entity_id, payload.received_at, persisted)
            return baseline

    def record(self, payload: ChangeEvent) -> EventOutcome:
        """Record one observed change, once per event ID."""
        if payload.source_id != self.source_id:
            return SOURCE_MISMATCH
        persisted = self.now().astimezone(UTC)
        if payload.received_at > persisted or payload.event_time > payload.received_at:
            return Rejected("future_timestamp", "Future event or receipt timestamp")
        with self.store.transaction() as db:
            if not Monitoring.admits(db, payload.entity_id, payload.selection_revision):
                return Rejected("selection_changed", "Selection no longer permits this event")
            previous = db.execute(
                "SELECT data FROM events WHERE source_id=? AND id=?", (self.source_id, payload.id)
            ).fetchone()
            row = EventRecord.model_validate(
                payload.model_dump(mode="json")
                | {
                    "device_kind": payload.device_kind,
                    "old": payload.old.stored() if payload.old else None,
                    "new": payload.new.stored() if payload.new else None,
                    "persisted_at": persisted.isoformat(),
                }
            )
            if previous:
                return _repeat(previous[0], row)
            old = payload.old.meaningful() if payload.old else None
            new = payload.new.meaningful() if payload.new else None
            if old == new:
                return "unchanged"
            db.execute(
                "INSERT INTO events VALUES(?, ?, ?, ?, ?, ?)",
                (
                    self.source_id,
                    payload.id,
                    payload.entity_id,
                    payload.event_time.isoformat(),
                    persisted.isoformat(),
                    stored_json(row),
                ),
            )
            # A change without a new state means Home Assistant dropped the entity.
            current_value = payload.new or device_state.validate_python(
                {"state": "unknown", "device_kind": payload.device_kind}
            )
            value = event_reading.validate_python(
                current_value.model_dump(mode="json", exclude_none=True)
                | {
                    "id": payload.id,
                    "entity_id": payload.entity_id,
                    "source_entity_id": payload.source_entity_id,
                    "source_id": self.source_id,
                    "kind": "event",
                    "event_time": payload.event_time.isoformat(),
                    "received_at": payload.received_at.isoformat(),
                    "persisted_at": persisted.isoformat(),
                }
            )
            update_current(db, payload.entity_id, payload.event_time.isoformat(), value)
            # Older deliveries may add evidence but cannot refresh a newer confirmation backwards.
            confirmed = db.execute(
                "SELECT received_at FROM confirmed WHERE entity_id=?", (payload.entity_id,)
            ).fetchone()
            if confirmed is None or payload.received_at.isoformat() >= confirmed[0]:
                self._confirm(db, payload.entity_id, payload.received_at, persisted)
            return "recorded"

    @staticmethod
    def _store_baseline(
        db: sqlite3.Connection,
        entity_id: str,
        data: dict[str, object],
        persisted: datetime,
        recovering: bool,
    ) -> BaselineRecord:
        """Store the snapshot unless it repeats the latest baseline; return what is stored."""
        previous = db.execute(
            "SELECT data FROM baselines WHERE entity_id=? ORDER BY rowid DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        baseline = baseline_record.validate_json(previous[0]) if previous else None
        comparable = _without(
            data,
            {"received_at", "kind", "coverage_gap_ids", "transition_time", "collection_started_at"},
        )
        old = baseline.model_dump(mode="json", include=set(comparable)) if baseline else None
        new_recovery = recovering and (
            not baseline or baseline.coverage_gap_ids != data.get("coverage_gap_ids")
        )
        if baseline is not None and comparable == old and not new_recovery:
            return baseline
        baseline = baseline_record.validate_python(
            data | {"id": str(uuid4()), "persisted_at": persisted.isoformat()}
        )
        db.execute(
            "INSERT INTO baselines(id,entity_id,data,persisted_at) VALUES(?, ?, ?, ?)",
            (
                baseline.id,
                entity_id,
                stored_json(baseline),
                persisted.isoformat(),
            ),
        )
        return baseline

    @staticmethod
    def _confirm(
        db: sqlite3.Connection, entity_id: str, received_at: datetime, persisted: datetime
    ) -> None:
        db.execute(
            "INSERT OR REPLACE INTO confirmed VALUES(?, ?, ?)",
            (entity_id, received_at.isoformat(), persisted.isoformat()),
        )


def _repeat(stored_row: str, row: EventRecord) -> Literal["duplicate"] | Rejected:
    # Both sides are read as records, so the stored row's JSON formatting does not matter.
    stored = EventRecord.model_validate_json(stored_row).model_dump(mode="json", exclude_unset=True)
    incoming = row.model_dump(mode="json", exclude_unset=True)
    # A stable source ID cannot silently refer to different evidence.
    ignored = EVENT_CONTEXT_KEYS | {"persisted_at"}
    if _without(stored, ignored) != _without(incoming, ignored):
        return Rejected("event_identity_conflict", "Event identity conflict")
    return "duplicate"


def _without(data: dict[str, object], keys: frozenset[str] | set[str]) -> dict[str, object]:
    return {key: value for key, value in data.items() if key not in keys}
