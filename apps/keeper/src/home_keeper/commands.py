"""Switches a monitored device through the controller and confirms the result by reading
the device's state from Home Assistant after the send.

Reads run in the shared thread pool and the controller send on its own few threads, so a
slow send never takes every worker the API's other requests need.
"""

import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import anyio
import anyio.to_thread
from anyio import CapacityLimiter
from pydantic import TypeAdapter, ValidationError

from home_keeper.control import Controller, ControllerUnreachable
from home_keeper.models import (
    CommandOutcome,
    CommandRecord,
    CommandRefusalCode,
    CommandRequest,
    CommandsPage,
    ControlStatus,
    DeviceKind,
    Identity,
    LightCommand,
    LightReading,
    Reading,
    to_utc,
)
from home_keeper.monitoring import UnknownEntity, UnsupportedEntity
from home_keeper.reader import LiveState, Reader, ReaderUnavailable
from home_keeper.store import Store, stored_json, stored_kind
from home_keeper_light_command import (
    ForwardedCommand,
    LightOff,
    forwarded_command,
    matches_brightness,
)

command_record: TypeAdapter[CommandRecord] = TypeAdapter(CommandRecord)


@dataclass(frozen=True)
class Refusal:
    code: CommandRefusalCode
    message: str


type Declined = Refusal | UnknownEntity | UnsupportedEntity


@dataclass(frozen=True)
class _Accepted:
    """A command that passed every check, with what sending and observing it need."""

    controller: Controller
    forwarded: ForwardedCommand


def shows(reading: Reading | None, command: LightCommand) -> bool:
    """Whether a light, plug or switch reading shows the state a command asks for."""
    if reading is None:
        return False
    if isinstance(command, LightOff):
        return reading.state == "off"
    if reading.state != "on":
        return False
    if command.brightness_pct is None:
        return True
    # Only a light has a brightness to match.
    return isinstance(reading, LightReading) and matches_brightness(
        reading.brightness, command.brightness_pct
    )


def _forward(
    device_kind: DeviceKind, source_entity_id: str, command: LightCommand
) -> ForwardedCommand:
    """The controller's body for a command. Raises ValidationError for an entity ID outside
    the device's own domain, or for a brightness on a plug or switch."""
    return forwarded_command.validate_python(
        {
            "device_kind": device_kind,
            "source_entity_id": source_entity_id,
            "command": command.model_dump(exclude_none=True),
        }
    )


class Commands:
    def __init__(
        self,
        store: Store,
        controller: Controller | None,
        reader: Reader,
        *,
        now: Callable[[], datetime],
        retention_days: int,
        wait_seconds: float,
        stale_seconds: float,
        poll_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        # Below the agent's 10-second request timeout, so a queued command never switches a
        # device after its caller gave up.
        queue_seconds: float = 3.0,
        slots: int = 4,
    ) -> None:
        """`wait_seconds`: how long, by `now`, to read the device's state after a send.
        `poll_seconds`: the pause between those reads, taken with `sleep`.
        `stale_seconds`: how old the controller's heartbeat may be while control stays on."""
        self.store = store
        self.controller = controller
        self.reader = reader
        self.now = now
        self.poll_seconds = poll_seconds
        self.sleep = sleep
        self.retention_days = retention_days
        self.wait_seconds = wait_seconds
        self.queue_seconds = queue_seconds
        self.stale_seconds = stale_seconds
        self._send_slots = CapacityLimiter(slots)
        # Controller sends can block for seconds, so they get their own few threads and
        # never take every worker of the shared pool that ingestion needs. One thread per
        # held slot, so a send never waits for a thread once it has a slot.
        self._send_threads = CapacityLimiter(slots)

    def heartbeat(self, received_at: datetime) -> bool:
        """Store the controller's heartbeat; False, with nothing stored, for a future receipt."""
        if received_at > self.now().astimezone(UTC):
            return False
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('control_heartbeat_at', ?)",
                (received_at.isoformat(),),
            )
        return True

    def status(self) -> ControlStatus:
        with self.store.transaction("DEFERRED") as db:
            return self._status(db)

    def interrupt_pending(self) -> None:
        """A command still pending was cut off by a restart; its outcome is unknown."""
        with self.store.transaction() as db:
            db.execute(
                "UPDATE commands SET data=json_set(data, '$.outcome', 'interrupted') "
                "WHERE json_extract(data, '$.outcome')='pending'"
            )

    async def switch(self, entity_id: str, request: CommandRequest) -> CommandRecord | Declined:
        checked = await anyio.to_thread.run_sync(self._accept, entity_id, request.command)
        if not isinstance(checked, _Accepted):
            return checked
        with anyio.move_on_after(self.queue_seconds) as queue:
            await self._send_slots.acquire()
        if queue.cancelled_caught:
            return Refusal("busy", "Too many light commands are waiting. Try again")
        try:
            # Checked again with the slot held: while the command waited, the device may
            # have stopped being monitored, or control may have turned off.
            accepted = await anyio.to_thread.run_sync(self._accept, entity_id, request.command)
            if not isinstance(accepted, _Accepted):
                return accepted
            sent_at = self.now().astimezone(UTC)
            # From the forwarded command, so a switch's record holds a switch command.
            record = command_record.validate_python(
                {
                    "id": str(uuid4()),
                    "entity_id": entity_id,
                    "device_kind": accepted.forwarded.device_kind,
                    "command": accepted.forwarded.command,
                    "sender": request.sender,
                    "sent_at": sent_at.isoformat(),
                    "outcome": "pending",
                }
            )
            # Stored before the send, so a crash during it still leaves the command on record.
            await anyio.to_thread.run_sync(self._save, record)
            try:
                result = await anyio.to_thread.run_sync(
                    accepted.controller.send, accepted.forwarded, limiter=self._send_threads
                )
            except ControllerUnreachable:
                result = None
        finally:
            self._send_slots.release()
        outcome: CommandOutcome
        if result is None:
            outcome = "unreachable"
        elif result == "refused":
            outcome = "refused"
        else:
            confirmed = await self._confirm(entity_id, request.command)
            outcome = "observed" if confirmed else "not_observed"
        record = record.model_copy(update={"outcome": outcome})
        await anyio.to_thread.run_sync(self._save, record)
        return record

    def history(
        self, *, start: datetime | None, end: datetime | None, limit: int, cursor: str | None
    ) -> CommandsPage | None:
        """Retained commands, newest first; None for a reversed range or an invalid cursor."""
        try:
            start = to_utc(start) if start else None
            end = to_utc(end) if end else None
            if start and end and start > end:
                raise ValueError("Reversed range")
            position = None
            if cursor is not None:
                timestamp, identity = cursor.split("|", 1)
                position = (
                    to_utc(datetime.fromisoformat(timestamp)).isoformat(),
                    TypeAdapter(Identity).validate_python(identity),
                )
        # OverflowError: a timestamp near year 1 or 9999 can fall outside UTC.
        except (ValueError, TypeError, OverflowError):
            return None
        clauses, parameters = ["sent_at>=?"], [self._retention_cutoff()]
        for operator, value in ((">=", start), ("<=", end)):
            if value is not None:
                clauses.append(f"sent_at {operator} ?")
                parameters.append(value.isoformat())
        if position:
            clauses.append("(sent_at < ? OR (sent_at=? AND id < ?))")
            parameters.extend((position[0], position[0], position[1]))
        with self.store.transaction("DEFERRED") as db:
            rows = db.execute(
                # Only fixed SQL fragments are concatenated; values are bound parameters.
                "SELECT data FROM commands WHERE "  # noqa: S608
                + " AND ".join(clauses)
                + " ORDER BY sent_at DESC, id DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        values = [command_record.validate_json(row[0]) for row in rows[:limit]]
        return CommandsPage(
            commands=values,
            next_cursor=f"{values[-1].sent_at}|{values[-1].id}" if len(rows) > limit else None,
            retained_since=self._retention_cutoff(),
        )

    def _retention_cutoff(self) -> str:
        return (self.now().astimezone(UTC) - timedelta(days=self.retention_days)).isoformat()

    def _status(self, db: sqlite3.Connection) -> ControlStatus:
        row = db.execute("SELECT value FROM metadata WHERE key='control_heartbeat_at'").fetchone()
        heartbeat_at: str | None = row[0] if row else None
        fresh = heartbeat_at is not None and (
            0
            <= (self.now() - datetime.fromisoformat(heartbeat_at)).total_seconds()
            < self.stale_seconds
        )
        # Without a configured controller, no heartbeat can turn control on.
        return ControlStatus(
            state="on" if fresh and self.controller is not None else "off",
            heartbeat_at=heartbeat_at,
        )

    def _accept(self, entity_id: str, command: LightCommand) -> _Accepted | Declined:  # noqa: PLR0911 -- one return per ordered check
        # Every refusal happens before anything is sent or stored.
        with self.store.transaction("DEFERRED") as db:
            selected = db.execute(
                "SELECT device_kind FROM selection WHERE id=?", (entity_id,)
            ).fetchone()
            control_on = self._status(db).state == "on"
        kind = stored_kind.validate_python(selected[0]) if selected else None
        try:
            entity = next((e for e in self.reader.catalog() if e.id == entity_id), None)
        except ReaderUnavailable:
            reachable = False
        else:
            reachable = True
            if entity is None:
                return UnknownEntity()
            if not entity.supported:
                return UnsupportedEntity()
            kind = entity.device_kind or kind
        # Refusal messages call a plug or switch a device.
        noun = "device" if kind == "switch" else "light"
        if self.controller is None or not control_on:
            return Refusal("control_off", "Light control is off")
        if selected is None:
            return Refusal("not_monitored", f"Monitor this {noun} before switching it")
        live: LiveState | None = None
        if reachable:
            try:
                live = self.reader.states([entity_id]).get(entity_id)
            except ReaderUnavailable:
                live = None
        if live is None or live.reading.state not in ("on", "off", "unavailable"):
            return Refusal("state_unknown", f"The {noun}'s current state is unknown")
        reading = live.reading
        # The reading's kind decides what can be forwarded, so it decides this check too.
        if (
            reading.device_kind == "switch"
            and not isinstance(command, LightOff)
            and command.brightness_pct is not None
        ):
            return Refusal("not_supported", "A plug or switch has no brightness")
        if reading.state == "unavailable":
            return Refusal("unavailable", f"The {noun} is unavailable")
        # Home Assistant shows no change for these, so the outcome could never be observed.
        if shows(reading, command):
            return Refusal("already_in_state", f"The {noun} is already in that state")
        try:
            forwarded = _forward(reading.device_kind, live.source_entity_id, command)
        except ValidationError:
            # Not an entity ID the controller takes for this kind of device.
            return UnsupportedEntity()
        return _Accepted(self.controller, forwarded)

    def _save(self, record: CommandRecord) -> None:
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO commands VALUES(?, ?, ?, ?)",
                (record.id, record.entity_id, record.sent_at, stored_json(record)),
            )

    async def _confirm(self, entity_id: str, command: LightCommand) -> bool:
        """Read the device's state every `poll_seconds` until it shows the command or
        `wait_seconds` have passed by `now`. A failed read counts as not showing it. The
        reads are also counted, so a clock that stalls or steps back cannot extend the wait."""
        deadline = self.now() + timedelta(seconds=self.wait_seconds)
        # One read at the send and one per pause, the last pause cut short at the deadline.
        reads = math.ceil(self.wait_seconds / self.poll_seconds - 1e-9) + 1
        for read in range(1, reads + 1):
            if await anyio.to_thread.run_sync(self._shows, entity_id, command):
                return True
            remaining = (deadline - self.now()).total_seconds()
            if remaining <= 0 or read == reads:
                break
            await anyio.to_thread.run_sync(self.sleep, min(self.poll_seconds, remaining))
        return False

    def _shows(self, entity_id: str, command: LightCommand) -> bool:
        try:
            live = self.reader.states([entity_id]).get(entity_id)
        except ReaderUnavailable:
            return False
        return live is not None and shows(live.reading, command)
