"""Device commands through `Commands`, with a real store and a scripted controller whose
steps run inside the send, so readings arrive during it without a delivery thread."""

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import anyio
import anyio.to_thread
import pytest
from pydantic import TypeAdapter

from home_keeper.commands import Commands, Refusal
from home_keeper.control import ControllerUnreachable
from home_keeper.models import (
    ChangeEvent,
    CommandRefusalCode,
    CommandRequest,
    CommandsPage,
    DeviceKind,
    DeviceObservation,
    Entity,
    LightCommandRecord,
    SwitchCommandRecord,
)
from home_keeper.monitoring import Monitoring, UnknownEntity, UnsupportedEntity
from home_keeper.recorder import Recorder, Rejected
from home_keeper.store import Store
from home_keeper_light_command import ForwardedCommand

pytestmark = pytest.mark.anyio

type Payload = dict[str, object]
type Recorded = LightCommandRecord | SwitchCommandRecord
type Result = Recorded | Refusal | UnknownEntity | UnsupportedEntity

NOW = datetime(2026, 10, 12, 10, tzinfo=UTC)
STALE_SECONDS = 15
# Long enough that a command never gives up on a reading the controller already delivered.
OBSERVE_WAIT = 5.0
# Short, for commands whose outcome is decided only when the wait runs out.
GIVE_UP_WAIT = 0.2

ON: Payload = {"action": "on"}
OFF: Payload = {"action": "off"}
DASHBOARD: Payload = {"kind": "dashboard"}
BROWSER_CHAT: Payload = {"kind": "browser_chat"}
TELEGRAM: Payload = {"kind": "telegram", "id": 424242, "name": "Ali <b>Telegram</b>"}

IS_OFF: Payload = {"state": "off"}
IS_ON: Payload = {"state": "on"}
IS_UNKNOWN: Payload = {"state": "unknown"}
IS_UNAVAILABLE: Payload = {"state": "unavailable"}

observation_of: TypeAdapter[DeviceObservation] = TypeAdapter(DeviceObservation)


def on(brightness_pct: int) -> Payload:
    return {"action": "on", "brightness_pct": brightness_pct}


@dataclass(frozen=True)
class Device:
    entity_id: str
    source: str
    kind: DeviceKind
    # What refusal messages call it.
    noun: str


LIGHT = Device("bulb-a", "light.desk", "light", "light")
PLUG = Device("plug-a", "switch.kettle", "switch", "device")
OTHER_LIGHT = Device("bulb-b", "light.other", "light", "light")
DEVICES = [pytest.param(LIGHT, id="light"), pytest.param(PLUG, id="plug")]

CATALOG = [
    Entity(id="bulb-a", device_id="device-a", name="Desk", device_kind="light", supported=True),
    Entity(id="bulb-b", device_id="device-b", name="Hall", device_kind="light", supported=True),
    Entity(id="plug-a", device_id="device-c", name="Kettle", device_kind="switch", supported=True),
    Entity(id="room", name="Living room", supported=False, reason="Groups are not recorded"),
    Entity(
        id="plug",
        name="Plug power",
        device_kind="switch",
        supported=False,
        reason="Only lights, plugs and switches are supported",
    ),
]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class ScriptedController:
    """Records each forwarded command and runs `steps` inside the send.

    A send moves the clock one second on before its steps run, so a reading a step records
    counts as received after the send.
    """

    clock: Clock
    sent: list[ForwardedCommand] = field(default_factory=list[ForwardedCommand])
    steps: list[Callable[[], object]] = field(default_factory=list[Callable[[], object]])
    reply: Literal["ok", "refused"] = "ok"
    unreachable: bool = False

    def send(self, forwarded: ForwardedCommand) -> Literal["ok", "refused"]:
        self.sent.append(forwarded)
        self.clock.now += timedelta(seconds=1)
        for step in self.steps:
            step()
        if self.unreachable:
            raise ControllerUnreachable
        return self.reply

    def bodies(self) -> list[Payload]:
        return [body.model_dump(exclude_none=True) for body in self.sent]


def kind_of(device: Device) -> Payload:
    return {"device_kind": "switch"} if device.kind == "switch" else {}


def bump_revision(db: sqlite3.Connection) -> None:
    db.execute("UPDATE metadata SET value=CAST(value AS INTEGER) + 1 WHERE key='revision'")


@dataclass
class Home:
    store: Store
    clock: Clock
    controller: ScriptedController
    recorder: Recorder
    commands: Commands

    def revision(self) -> int:
        with self.store.transaction("DEFERRED") as db:
            return Store.selection(db).revision

    def select(self, device: Device, *, selected: bool = True) -> None:
        monitoring = Monitoring(self.store, now=self.clock, reconciliation_seconds=60)
        result = monitoring.select(device.entity_id, selected=selected)
        assert not isinstance(result, UnknownEntity | UnsupportedEntity), result

    def snapshot(
        self,
        device: Device,
        state: Payload,
        *,
        at: datetime | None = None,
        source: str | None = None,
    ) -> None:
        """Record a snapshot as the collector would, received at `at` (default: now)."""
        payload = observation_of.validate_python(
            {
                "entity_id": device.entity_id,
                "source_entity_id": source or device.source,
                "selection_revision": self.revision(),
                "source_id": "demo",
                "received_at": at or self.clock.now,
                # Source times before every test reading, so a later event always wins.
                "last_changed": "2020-01-01T00:00:00Z",
                "last_updated": "2020-01-01T00:00:00Z",
                "availability": "available" if state["state"] in ("on", "off") else state["state"],
            }
            | kind_of(device)
            | state
        )
        result = self.recorder.observe(payload)
        assert not isinstance(result, Rejected), result

    def report(
        self,
        device: Device,
        identity: str,
        new: Payload,
        *,
        old: Payload,
        at: datetime | None = None,
        source: str | None = None,
    ) -> None:
        """Record a change event as the collector would, received at `at` (default: now)."""
        received = at or self.clock.now
        payload = ChangeEvent.model_validate(
            {
                "id": identity,
                "entity_id": device.entity_id,
                "source_entity_id": source or device.source,
                "source_id": "demo",
                "selection_revision": self.revision(),
                "event_time": received,
                "received_at": received,
                "old": old | kind_of(device),
                "new": new | kind_of(device),
            }
        )
        assert self.recorder.record(payload) == "recorded"

    def page(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ) -> CommandsPage:
        page = self.commands.history(start=start, end=end, limit=limit, cursor=cursor)
        assert page is not None
        return page

    def audit(self) -> list[Recorded]:
        return self.page().commands


def home(
    tmp_path: Path,
    *,
    controller: bool = True,
    wait: float = GIVE_UP_WAIT,
    queue: float = 3.0,
    slots: int = 4,
    retention_days: int = 30,
) -> Home:
    """The test catalog with bulb-a and plug-a monitored, no readings and no heartbeat."""
    clock = Clock()
    store = Store(tmp_path / "commands.sqlite")
    with store.transaction() as db:
        # Written by `create_app` at startup; the selection read needs it.
        db.execute("INSERT INTO metadata VALUES('refresh_revision', '0')")
        db.executemany(
            "INSERT INTO catalog VALUES(?, ?)",
            [(entity.id, entity.model_dump_json()) for entity in CATALOG],
        )
        db.executemany(
            "INSERT INTO selection(id, device_kind) VALUES(?, ?)",
            [(device.entity_id, device.kind) for device in (LIGHT, PLUG)],
        )
        bump_revision(db)
    scripted = ScriptedController(clock)
    commands = Commands(
        store,
        scripted if controller else None,
        now=clock,
        retention_days=retention_days,
        wait_seconds=wait,
        queue_seconds=queue,
        slots=slots,
        stale_seconds=STALE_SECONDS,
    )
    return Home(store, clock, scripted, Recorder(store, "demo", clock), commands)


def ready(
    tmp_path: Path,
    device: Device,
    state: Payload = IS_OFF,
    *,
    wait: float = GIVE_UP_WAIT,
    queue: float = 3.0,
    slots: int = 4,
    retention_days: int = 30,
) -> Home:
    """A home with control on and `device` last read in `state`."""
    house = home(tmp_path, wait=wait, queue=queue, slots=slots, retention_days=retention_days)
    house.snapshot(device, state)
    assert house.commands.heartbeat(NOW)
    return house


async def switch(
    house: Home, device: Device | str, action: Payload, sender: Payload = DASHBOARD
) -> Result:
    entity_id = device if isinstance(device, str) else device.entity_id
    request = CommandRequest.model_validate({"command": action, "sender": sender})
    return await house.commands.switch(entity_id, request)


def recorded(result: Result) -> Recorded:
    assert isinstance(result, LightCommandRecord | SwitchCommandRecord), result
    return result


def forwarded(device: Device, action: Payload, source: str | None = None) -> Payload:
    """The body the controller receives for `action` on `device`."""
    return {
        "device_kind": device.kind,
        "source_entity_id": source or device.source,
        "command": action,
    }


def refused(code: CommandRefusalCode, device: Device = LIGHT) -> Refusal:
    """The refusal for `code`, with the message the API has always returned for it."""
    messages: dict[CommandRefusalCode, str] = {
        "control_off": "Light control is off",
        "not_monitored": f"Monitor this {device.noun} before switching it",
        "state_unknown": f"The {device.noun}'s current state is unknown",
        "unavailable": f"The {device.noun} is unavailable",
        "already_in_state": f"The {device.noun} is already in that state",
        "not_supported": "A plug or switch has no brightness",
        "busy": "Too many light commands are waiting. Try again",
    }
    return Refusal(code, messages[code])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# Control status


async def test_control_is_on_only_while_the_controller_heartbeat_is_fresh(tmp_path: Path) -> None:
    house = home(tmp_path)
    house.snapshot(LIGHT, IS_OFF)
    assert house.commands.status().model_dump() == {"state": "off", "heartbeat_at": None}
    assert await switch(house, LIGHT, ON) == refused("control_off")

    assert house.commands.heartbeat(NOW)
    status = house.commands.status()
    assert status.state == "on"
    assert status.heartbeat_at is not None
    assert datetime.fromisoformat(status.heartbeat_at) == NOW

    house.clock.now = NOW + timedelta(seconds=STALE_SECONDS - 1)
    assert house.commands.status().state == "on"

    house.clock.now = NOW + timedelta(seconds=STALE_SECONDS + 1)
    stale = house.commands.status()
    assert stale.state == "off"
    assert stale.heartbeat_at is not None
    assert datetime.fromisoformat(stale.heartbeat_at) == NOW
    assert await switch(house, LIGHT, ON) == refused("control_off")
    assert house.controller.sent == []

    assert house.commands.heartbeat(house.clock.now)
    assert house.commands.status().state == "on"
    recorded(await switch(house, LIGHT, ON))
    assert len(house.controller.sent) == 1


async def test_heartbeat_received_after_now_is_not_stored(tmp_path: Path) -> None:
    house = home(tmp_path)
    assert not house.commands.heartbeat(NOW + timedelta(seconds=1))
    assert house.commands.status().model_dump() == {"state": "off", "heartbeat_at": None}


async def test_without_a_controller_control_is_off_and_commands_are_refused(
    tmp_path: Path,
) -> None:
    house = home(tmp_path, controller=False)
    house.snapshot(LIGHT, IS_OFF)
    # Whether or not the heartbeat is stored, no controller means no control.
    house.commands.heartbeat(NOW)
    assert house.commands.status().state == "off"
    assert await switch(house, LIGHT, ON) == refused("control_off")
    assert house.audit() == []


# Refusals


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        ("missing", UnknownEntity),
        # The Home Assistant entity ID is not a catalog ID.
        ("light.desk", UnknownEntity),
        # Injected characters are only an unknown catalog ID.
        ("bulb-a;DROP TABLE events", UnknownEntity),
        ("bulb-a' OR '1'='1", UnknownEntity),
        ("room", UnsupportedEntity),
        ("plug", UnsupportedEntity),
    ],
)
async def test_unknown_or_unsupported_entity_is_reported_before_control_is_checked(
    tmp_path: Path, entity: str, expected: type[UnknownEntity] | type[UnsupportedEntity]
) -> None:
    # Control is off, so any other answer means the catalog checks did not come first.
    house = home(tmp_path)
    house.snapshot(LIGHT, IS_OFF)
    assert isinstance(await switch(house, entity, ON), expected)
    assert house.controller.sent == []
    assert house.audit() == []


def refusal_cases() -> list[object]:
    """(device, action, last reading, monitored, heartbeat, refusal code), in check order."""
    shared: list[tuple[str, Payload, Payload | None, bool, bool, CommandRefusalCode]] = [
        ("control off", ON, IS_OFF, True, False, "control_off"),
        # Control is checked before monitoring.
        ("control off and not monitored", ON, None, False, False, "control_off"),
        ("not monitored", ON, None, False, True, "not_monitored"),
        ("never read", ON, None, True, True, "state_unknown"),
        ("unknown state", ON, IS_UNKNOWN, True, True, "state_unknown"),
        ("unavailable, on", ON, IS_UNAVAILABLE, True, True, "unavailable"),
        ("unavailable, off", OFF, IS_UNAVAILABLE, True, True, "unavailable"),
        ("already off", OFF, IS_OFF, True, True, "already_in_state"),
        ("already on", ON, IS_ON, True, True, "already_in_state"),
    ]
    return [
        *(
            pytest.param(device, *case[1:], id=f"{device.kind}: {case[0]}")
            for device in (LIGHT, PLUG)
            for case in shared
        ),
        # 30 % is brightness round(76.5) = 77; 75 to 79 already match it.
        pytest.param(
            LIGHT,
            on(30),
            {"state": "on", "brightness": 76},
            True,
            True,
            "already_in_state",
            id="light: two steps below",
        ),
        pytest.param(
            LIGHT,
            on(30),
            {"state": "on", "brightness": 78},
            True,
            True,
            "already_in_state",
            id="light: one step above",
        ),
        # A plug has no brightness, so no brightness command can be carried out on it.
        pytest.param(PLUG, on(30), IS_OFF, True, True, "not_supported", id="switch: brightness"),
        pytest.param(PLUG, on(100), IS_OFF, True, True, "not_supported", id="switch: full"),
        # Brightness support is checked before availability.
        pytest.param(
            PLUG,
            on(30),
            IS_UNAVAILABLE,
            True,
            True,
            "not_supported",
            id="switch: brightness while unavailable",
        ),
    ]


@pytest.mark.parametrize(
    ("device", "action", "reading", "monitored", "heartbeat", "code"), refusal_cases()
)
async def test_refusals_come_in_a_fixed_order_and_nothing_is_sent_or_stored(
    tmp_path: Path,
    *,
    device: Device,
    action: Payload,
    reading: Payload | None,
    monitored: bool,
    heartbeat: bool,
    code: CommandRefusalCode,
) -> None:
    house = home(tmp_path)
    if reading is not None:
        house.snapshot(device, reading)
    if not monitored:
        house.select(device, selected=False)
    if heartbeat:
        assert house.commands.heartbeat(NOW)
    assert await switch(house, device, action) == refused(code, device)
    assert house.controller.sent == []
    assert house.audit() == []


async def test_a_reselected_device_refuses_commands_until_a_new_reading_arrives(
    tmp_path: Path,
) -> None:
    house = ready(tmp_path, LIGHT, IS_OFF)
    house.select(LIGHT, selected=False)
    house.select(LIGHT)
    assert await switch(house, LIGHT, ON) == refused("state_unknown")
    assert house.controller.sent == []


async def test_brightness_is_refused_for_a_switch_reading_even_when_discovery_says_light(
    tmp_path: Path,
) -> None:
    house = home(tmp_path)
    # Discovery lists bulb-a as a light; Home Assistant now reports it as a switch.
    house.snapshot(Device("bulb-a", "switch.desk", "switch", "device"), IS_OFF)
    assert house.commands.heartbeat(NOW)
    assert await switch(house, LIGHT, on(30)) == refused("not_supported")
    assert house.controller.sent == []


async def test_reading_from_outside_the_device_domain_makes_the_entity_unsupported(
    tmp_path: Path,
) -> None:
    # The controller only takes a light's own `light.*` entity ID.
    house = home(tmp_path)
    house.snapshot(LIGHT, IS_OFF, source="switch.desk")
    assert house.commands.heartbeat(NOW)
    assert isinstance(await switch(house, LIGHT, ON), UnsupportedEntity)
    assert house.controller.sent == []
    assert house.audit() == []


# Forwarding


@pytest.mark.parametrize(
    ("device", "reading", "action"),
    [
        pytest.param(LIGHT, IS_OFF, ON, id="light on"),
        pytest.param(LIGHT, IS_OFF, on(30), id="light on at 30 %"),
        pytest.param(LIGHT, IS_ON, OFF, id="light off"),
        # A different brightness is a change: 74 is three steps from 77.
        pytest.param(LIGHT, {"state": "on", "brightness": 74}, on(30), id="light dim to 30 %"),
        pytest.param(LIGHT, {"state": "on", "brightness": 100}, on(30), id="light from full"),
        # A light with no recorded brightness cannot already be at 30 %.
        pytest.param(LIGHT, {"state": "on", "color_mode": "onoff"}, on(30), id="light no level"),
        pytest.param(PLUG, IS_OFF, ON, id="plug on"),
        pytest.param(PLUG, IS_ON, OFF, id="plug off"),
    ],
)
async def test_change_is_forwarded_to_the_source_entity_of_the_current_reading(
    tmp_path: Path, device: Device, reading: Payload, action: Payload
) -> None:
    house = ready(tmp_path, device, reading)
    record = recorded(await switch(house, device, action))
    assert house.controller.bodies() == [forwarded(device, action)]
    assert record.device_kind == device.kind


@pytest.mark.parametrize("device", DEVICES)
async def test_forwarding_follows_the_source_entity_id_of_the_newest_reading(
    tmp_path: Path, device: Device
) -> None:
    house = ready(tmp_path, device)
    # The latest event attributes the device to a renamed Home Assistant entity.
    renamed = f"{device.source}_2"
    house.report(device, "renamed", IS_OFF, old=IS_ON, source=renamed)
    recorded(await switch(house, device, ON))
    assert house.controller.bodies() == [forwarded(device, ON, renamed)]


# Observing the result


@pytest.mark.parametrize(
    ("device", "action", "shown"),
    [
        pytest.param(LIGHT, on(30), {"state": "on", "brightness": 77}, id="light"),
        pytest.param(PLUG, ON, IS_ON, id="plug"),
    ],
)
async def test_command_is_observed_from_a_matching_event_received_after_the_send(
    tmp_path: Path, device: Device, action: Payload, shown: Payload
) -> None:
    house = ready(tmp_path, device, wait=OBSERVE_WAIT)
    house.controller.steps.append(lambda: house.report(device, "after-send", shown, old=IS_OFF))
    record = recorded(await switch(house, device, action, TELEGRAM))
    assert record.model_dump(mode="json", exclude={"id", "sent_at", "command"}) == {
        "entity_id": device.entity_id,
        "device_kind": device.kind,
        "sender": TELEGRAM,
        "outcome": "observed",
        "event_id": "after-send",
    }
    assert record.command.model_dump(exclude_none=True) == action
    # The send time is taken before the controller is called.
    assert datetime.fromisoformat(record.sent_at) == NOW
    assert house.controller.bodies() == [forwarded(device, action)]
    assert house.audit() == [record]


@pytest.mark.parametrize("device", DEVICES)
async def test_off_command_is_observed_from_an_off_event(tmp_path: Path, device: Device) -> None:
    house = ready(tmp_path, device, IS_ON, wait=OBSERVE_WAIT)
    house.controller.steps.append(lambda: house.report(device, "went-off", IS_OFF, old=IS_ON))
    record = recorded(await switch(house, device, OFF, BROWSER_CHAT))
    assert (record.outcome, record.event_id) == ("observed", "went-off")


@pytest.mark.parametrize("device", DEVICES)
async def test_command_is_observed_from_a_snapshot_received_after_the_send(
    tmp_path: Path, device: Device
) -> None:
    house = ready(tmp_path, device, wait=OBSERVE_WAIT)
    house.controller.steps.append(lambda: house.snapshot(device, IS_ON))
    record = recorded(await switch(house, device, ON))
    # A snapshot is evidence but not an event.
    assert (record.outcome, record.event_id) == ("observed", None)


async def test_matching_reading_that_arrives_after_the_send_returns_is_observed(
    tmp_path: Path,
) -> None:
    house = ready(tmp_path, LIGHT, wait=OBSERVE_WAIT)
    sent = threading.Event()
    house.controller.steps.append(sent.set)
    results: list[Result] = []

    async def command() -> None:
        results.append(await switch(house, LIGHT, ON))

    async with anyio.create_task_group() as group:
        group.start_soon(command)
        await anyio.to_thread.run_sync(sent.wait, 5)
        # The command now waits for its result; the light reports a moment later.
        await anyio.sleep(0.3)
        house.report(LIGHT, "later", IS_ON, old=IS_OFF)
    [result] = results
    record = recorded(result)
    assert (record.outcome, record.event_id) == ("observed", "later")


@pytest.mark.parametrize(
    ("brightness", "outcome"),
    [(76, "observed"), (78, "observed"), (79, "observed"), (74, "not_observed")],
)
async def test_brightness_within_two_steps_of_the_requested_percent_counts_as_observed(
    tmp_path: Path, brightness: int, outcome: str
) -> None:
    house = ready(tmp_path, LIGHT)
    shown: Payload = {"state": "on", "brightness": brightness}
    house.controller.steps.append(lambda: house.report(LIGHT, "dimmed", shown, old=IS_OFF))
    record = recorded(await switch(house, LIGHT, on(30)))
    assert record.outcome == outcome
    assert record.event_id == ("dimmed" if outcome == "observed" else None)


async def test_waiting_continues_past_a_non_matching_reading(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT, wait=OBSERVE_WAIT)
    ramping: Payload = {"state": "on", "brightness": 20}

    def settle() -> None:
        house.report(LIGHT, "ramping", ramping, old=IS_OFF)
        house.report(LIGHT, "settled", {"state": "on", "brightness": 77}, old=ramping)

    house.controller.steps.append(settle)
    record = recorded(await switch(house, LIGHT, on(30)))
    assert (record.outcome, record.event_id) == ("observed", "settled")


@pytest.mark.parametrize("reading", ["event", "snapshot"])
async def test_reading_received_before_the_send_never_counts(tmp_path: Path, reading: str) -> None:
    house = home(tmp_path)
    house.snapshot(LIGHT, IS_OFF, at=NOW - timedelta(seconds=10))
    assert house.commands.heartbeat(NOW)
    before_send = NOW - timedelta(seconds=1)

    def late_delivery() -> None:
        # The collector received this change before the send but stores it during it.
        if reading == "event":
            house.report(LIGHT, "late-delivery", IS_ON, old=IS_OFF, at=before_send)
        else:
            house.snapshot(LIGHT, IS_ON, at=before_send)

    house.controller.steps.append(late_delivery)
    record = recorded(await switch(house, LIGHT, ON))
    assert (record.outcome, record.event_id) == ("not_observed", None)


async def test_matching_reading_of_another_light_does_not_count(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT)
    house.select(OTHER_LIGHT)
    house.snapshot(OTHER_LIGHT, IS_OFF)
    house.controller.steps.append(
        lambda: house.report(OTHER_LIGHT, "other-light", IS_ON, old=IS_OFF)
    )
    record = recorded(await switch(house, LIGHT, ON))
    assert record.outcome == "not_observed"


@pytest.mark.parametrize("device", DEVICES)
async def test_device_that_reports_no_change_is_not_observed(
    tmp_path: Path, device: Device
) -> None:
    house = ready(tmp_path, device)
    # Home Assistant took the call, but the device reports the same state again.
    house.controller.steps.append(lambda: house.snapshot(device, IS_OFF))
    record = recorded(await switch(house, device, ON))
    assert (record.outcome, record.event_id) == ("not_observed", None)
    assert house.audit() == [record]


@pytest.mark.parametrize("reading", ["event", "snapshot"])
@pytest.mark.parametrize("cleanup", [False, True], ids=["kept", "cleaned"])
async def test_matching_reading_recorded_after_retention_cleanup_is_observed(
    tmp_path: Path, reading: str, *, cleanup: bool
) -> None:
    house = home(tmp_path, retention_days=1, wait=OBSERVE_WAIT)
    # The light's only stored history sits exactly at the retention boundary at the send.
    house.snapshot(LIGHT, IS_OFF)
    if reading == "event":
        house.report(LIGHT, "old-off", IS_OFF, old=IS_ON)
    house.clock.now = NOW + timedelta(days=1)
    assert house.commands.heartbeat(house.clock.now)

    def expire_then_deliver() -> None:
        # Runs during the send, after the clock moved on: the old history expires.
        if cleanup:
            with house.store.transaction() as db:
                Store.cleanup(db, (house.clock.now - timedelta(days=1)).isoformat())
        if reading == "event":
            house.report(LIGHT, "new-on", IS_ON, old=IS_OFF)
        else:
            house.snapshot(LIGHT, IS_ON)

    house.controller.steps.append(expire_then_deliver)
    record = recorded(await switch(house, LIGHT, ON))
    assert (record.outcome, record.event_id) == (
        "observed",
        "new-on" if reading == "event" else None,
    )
    assert house.audit() == [record]


async def test_no_matching_reading_within_the_wait_is_not_observed(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT, wait=0.4)
    started = time.monotonic()
    record = recorded(await switch(house, LIGHT, ON))
    assert time.monotonic() - started >= 0.4
    assert (record.outcome, record.event_id) == ("not_observed", None)
    assert house.audit() == [record]


@pytest.mark.parametrize("outcome", ["refused", "unreachable"])
@pytest.mark.parametrize("device", DEVICES)
async def test_controller_that_refuses_or_fails_is_audited_without_waiting(
    tmp_path: Path, device: Device, outcome: str
) -> None:
    house = ready(tmp_path, device, wait=OBSERVE_WAIT)
    house.controller.reply = "refused"
    house.controller.unreachable = outcome == "unreachable"
    started = time.monotonic()
    record = recorded(await switch(house, device, ON))
    assert time.monotonic() - started < OBSERVE_WAIT - 1
    assert (record.outcome, record.event_id) == (outcome, None)
    assert house.controller.bodies() == [forwarded(device, ON)]
    assert house.audit() == [record]


# The stored record


async def test_command_is_stored_as_pending_while_the_controller_works(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT)
    during_send: list[list[Recorded]] = []
    house.controller.steps.append(lambda: during_send.append(house.audit()))
    record = recorded(await switch(house, LIGHT, on(30), TELEGRAM))
    assert during_send == [[record.model_copy(update={"outcome": "pending", "event_id": None})]]
    assert record.outcome == "not_observed"
    assert house.audit() == [record]


class SimulatedCrashError(Exception):
    """Stands in for the API process dying while the controller works."""


async def test_command_left_pending_by_a_crash_is_interrupted_after_restart(
    tmp_path: Path,
) -> None:
    house = ready(tmp_path, LIGHT)

    def crash() -> None:
        raise SimulatedCrashError

    house.controller.steps.append(crash)
    with pytest.raises(SimulatedCrashError):
        await switch(house, LIGHT, on(30), TELEGRAM)
    [left] = house.audit()
    assert left.outcome == "pending"
    assert (left.entity_id, left.command.model_dump(exclude_none=True)) == ("bulb-a", on(30))
    assert left.sender.model_dump() == TELEGRAM
    assert datetime.fromisoformat(left.sent_at) == NOW

    restarted = Commands(
        house.store,
        ScriptedController(house.clock),
        now=house.clock,
        retention_days=30,
        wait_seconds=5.0,
        stale_seconds=STALE_SECONDS,
    )
    restarted.interrupt_pending()
    page = restarted.history(start=None, end=None, limit=500, cursor=None)
    assert page is not None
    assert page.commands == [left.model_copy(update={"outcome": "interrupted"})]


# Send slots


def hold_the_only_slot(house: Home) -> tuple[threading.Event, threading.Event]:
    """Make the next send block until `release` is set; `entered` is set once it blocks."""
    entered, release = threading.Event(), threading.Event()

    def block() -> None:
        entered.set()
        release.wait(10)

    house.controller.steps.append(block)
    return entered, release


async def test_command_that_waits_too_long_for_a_send_slot_is_refused_as_busy(
    tmp_path: Path,
) -> None:
    house = ready(tmp_path, LIGHT, slots=1, queue=0.2)
    entered, release = hold_the_only_slot(house)
    first: list[Result] = []

    async def command() -> None:
        first.append(await switch(house, LIGHT, ON))

    async with anyio.create_task_group() as group:
        group.start_soon(command)
        await anyio.to_thread.run_sync(entered.wait, 5)
        try:
            # The only slot stays held until this command gives up.
            queued = await switch(house, LIGHT, ON)
        finally:
            release.set()
        assert queued == refused("busy")
    assert len(house.controller.sent) == 1
    assert house.audit() == [recorded(first[0])]


async def test_queued_command_is_not_sent_after_its_device_stops_being_monitored(
    tmp_path: Path,
) -> None:
    house = ready(tmp_path, LIGHT, slots=1, queue=5)
    entered, release = hold_the_only_slot(house)
    first: list[Result] = []
    queued: list[Result] = []

    async def command(results: list[Result]) -> None:
        results.append(await switch(house, LIGHT, ON))

    async with anyio.create_task_group() as group:
        group.start_soon(command, first)
        await anyio.to_thread.run_sync(entered.wait, 5)
        group.start_soon(command, queued)
        # Long enough for the second command to pass its first check and wait for the slot.
        await anyio.sleep(0.3)
        assert queued == [], "the second command did not wait for the send slot"
        house.select(LIGHT, selected=False)
        release.set()
    assert queued == [refused("not_monitored")]
    assert len(house.controller.sent) == 1
    assert house.audit() == [recorded(first[0])]


# The command audit


@pytest.mark.parametrize(
    "sender", [DASHBOARD, BROWSER_CHAT, TELEGRAM], ids=["dashboard", "chat", "telegram"]
)
async def test_sender_round_trips_through_the_audit(tmp_path: Path, sender: Payload) -> None:
    house = ready(tmp_path, LIGHT)
    house.controller.reply = "refused"
    record = recorded(await switch(house, LIGHT, ON, sender))
    assert record.sender.model_dump() == sender
    assert [stored.sender.model_dump() for stored in house.audit()] == [sender]


async def test_the_audit_lists_light_and_switch_commands_with_their_device_kind(
    tmp_path: Path,
) -> None:
    house = ready(tmp_path, LIGHT)
    house.snapshot(PLUG, IS_OFF)
    recorded(await switch(house, LIGHT, on(40), TELEGRAM))
    recorded(await switch(house, PLUG, ON, DASHBOARD))
    audit = {
        record.entity_id: (
            record.device_kind,
            record.command.model_dump(exclude_none=True),
            record.sender.model_dump(),
        )
        for record in house.audit()
    }
    assert audit == {
        "bulb-a": ("light", on(40), TELEGRAM),
        "plug-a": ("switch", ON, DASHBOARD),
    }
    assert house.controller.bodies() == [forwarded(LIGHT, on(40)), forwarded(PLUG, ON)]


async def test_history_is_newest_first_in_pages_and_filters_by_send_time(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT)
    # Refused commands leave the light off, so the same command stays valid.
    house.controller.reply = "refused"
    sent: list[Recorded] = []
    for _ in range(3):
        # The commands span longer than the stale limit, so keep the heartbeat fresh.
        assert house.commands.heartbeat(house.clock.now)
        sent.append(recorded(await switch(house, LIGHT, ON)))
        house.clock.now += timedelta(seconds=9)
    times = [datetime.fromisoformat(record.sent_at) for record in sent]
    assert times == [NOW, NOW + timedelta(seconds=10), NOW + timedelta(seconds=20)]

    first = house.page(limit=2)
    assert first.commands == [sent[2], sent[1]]
    assert first.next_cursor is not None
    rest = house.page(limit=2, cursor=first.next_cursor)
    assert rest.commands == [sent[0]]
    assert rest.next_cursor is None

    assert house.page(start=times[1]).commands == [sent[2], sent[1]]
    assert house.page(end=times[1]).commands == [sent[1], sent[0]]
    assert house.page(start=times[1], end=times[1]).commands == [sent[1]]
    assert house.page().commands == [sent[2], sent[1], sent[0]]


@pytest.mark.parametrize(
    ("start", "end", "cursor"),
    [
        pytest.param(NOW, NOW - timedelta(seconds=1), None, id="reversed range"),
        pytest.param(None, None, "not-a-cursor", id="cursor without a separator"),
        pytest.param(None, None, "2026-10-12T10:00:00+00:00|not an id", id="cursor bad id"),
        # Year 1 with a positive offset falls before the first UTC instant.
        pytest.param(None, None, "0001-01-01T00:00:00+14:00|light.desk", id="cursor before UTC"),
    ],
)
async def test_invalid_history_scope_reads_nothing(
    tmp_path: Path, start: datetime | None, end: datetime | None, cursor: str | None
) -> None:
    house = home(tmp_path)
    assert house.commands.history(start=start, end=end, limit=50, cursor=cursor) is None


async def test_commands_older_than_retention_are_hidden_then_cleaned_up(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT, retention_days=1)
    house.controller.reply = "refused"
    old = recorded(await switch(house, LIGHT, ON))
    house.clock.now = NOW + timedelta(days=1, seconds=30)
    assert house.commands.heartbeat(house.clock.now)
    recent = recorded(await switch(house, LIGHT, ON))

    page = house.page()
    assert page.commands == [recent]
    assert page.retained_since == (house.clock.now - timedelta(days=1)).isoformat()

    with house.store.transaction() as db:
        Store.cleanup(db, (NOW + timedelta(seconds=1)).isoformat())
    # Back inside the retention window, only the deleted row stays missing.
    house.clock.now = NOW
    assert old not in house.audit()
    assert house.audit() == [recent]
