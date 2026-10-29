"""Device commands through `Commands`, with a real store, a fake reader and a scripted
controller whose steps run inside the send.

Time is the test's: `now` is a settable clock and `sleep` moves it, so the confirmation
reads after a send happen at known times and no test waits in real time.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Literal

import anyio
import anyio.to_thread
import pytest
from support.reader import FakeReader, Payload

from home_keeper.commands import Commands, Refusal
from home_keeper.control import ControllerUnreachable
from home_keeper.models import (
    CommandRefusalCode,
    CommandRequest,
    CommandsPage,
    DeviceKind,
    Entity,
    LightCommandRecord,
    SwitchCommandRecord,
)
from home_keeper.monitoring import UnknownEntity, UnsupportedEntity
from home_keeper.store import Store
from home_keeper_light_command import ForwardedCommand

pytestmark = pytest.mark.anyio

type Recorded = LightCommandRecord | SwitchCommandRecord
type Result = Recorded | Refusal | UnknownEntity | UnsupportedEntity

NOW = datetime(2026, 10, 12, 10, tzinfo=UTC)
STALE_SECONDS = 15
WAIT_SECONDS = 5.0
POLL_SECONDS = 0.5

ON: Payload = {"action": "on"}
OFF: Payload = {"action": "off"}
DASHBOARD: Payload = {"kind": "dashboard"}
BROWSER_CHAT: Payload = {"kind": "browser_chat"}
TELEGRAM: Payload = {"kind": "telegram", "id": 424242, "name": "Ali <b>Telegram</b>"}

IS_OFF: Payload = {"state": "off"}
IS_ON: Payload = {"state": "on"}
IS_UNKNOWN: Payload = {"state": "unknown"}
IS_UNAVAILABLE: Payload = {"state": "unavailable"}


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
        # Every sleep the command asked for, in seconds.
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


@dataclass
class ScriptedController:
    """Records each forwarded command and runs `steps` inside the send. The clock stays put."""

    sent: list[ForwardedCommand] = field(default_factory=list[ForwardedCommand])
    steps: list[Callable[[], object]] = field(default_factory=list[Callable[[], object]])
    reply: Literal["ok", "refused"] = "ok"
    unreachable: bool = False

    def send(self, forwarded: ForwardedCommand) -> Literal["ok", "refused"]:
        self.sent.append(forwarded)
        for step in self.steps:
            step()
        if self.unreachable:
            raise ControllerUnreachable
        return self.reply

    def bodies(self) -> list[Payload]:
        return [body.model_dump(exclude_none=True) for body in self.sent]


def kind_of(device: Device) -> Payload:
    return {"device_kind": "switch"} if device.kind == "switch" else {}


@dataclass
class Home:
    store: Store
    clock: Clock
    controller: ScriptedController
    reader: FakeReader
    commands: Commands
    # The clock when the controller was called; None before the first send.
    sent_at: datetime | None = None
    # Seconds after the send at which each later state read started.
    confirmation_reads: list[float] = field(default_factory=list[float])
    # Run at each state read after the send, before the reader answers.
    after_send: list[Callable[[], object]] = field(default_factory=list[Callable[[], object]])

    def __post_init__(self) -> None:
        def mark_send() -> None:
            self.sent_at = self.clock.now

        def on_read() -> None:
            if self.sent_at is None:
                return
            self.confirmation_reads.append((self.clock.now - self.sent_at).total_seconds())
            for step in self.after_send:
                step()

        self.controller.steps.insert(0, mark_send)
        self.reader.on_read = on_read

    def select(self, device: Device, *, selected: bool = True) -> None:
        with self.store.transaction() as db:
            if selected:
                db.execute(
                    "INSERT OR IGNORE INTO selection(id, device_kind) VALUES(?, ?)",
                    (device.entity_id, device.kind),
                )
            else:
                db.execute("DELETE FROM selection WHERE id=?", (device.entity_id,))

    def report(self, device: Device, state: Payload, *, source: str | None = None) -> None:
        """Home Assistant now reports `state` for the device."""
        self.reader.set(device.entity_id, source or device.source, state | kind_of(device))

    def report_from(self, seconds: float, device: Device, state: Payload) -> None:
        """Home Assistant reports `state` from `seconds` after the send on."""

        def change() -> None:
            sent_at = self.sent_at
            if sent_at is not None and self.clock.now >= sent_at + timedelta(seconds=seconds):
                self.report(device, state)

        self.after_send.append(change)

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


def make_commands(
    store: Store,
    controller: ScriptedController | None,
    reader: FakeReader,
    clock: Clock,
    *,
    queue: float = 3.0,
    slots: int = 4,
    retention_days: int = 30,
) -> Commands:
    return Commands(
        store,
        controller,
        reader,
        now=clock,
        retention_days=retention_days,
        wait_seconds=WAIT_SECONDS,
        stale_seconds=STALE_SECONDS,
        poll_seconds=POLL_SECONDS,
        sleep=clock.sleep,
        queue_seconds=queue,
        slots=slots,
    )


def home(
    tmp_path: Path,
    *,
    controller: bool = True,
    queue: float = 3.0,
    slots: int = 4,
    retention_days: int = 30,
) -> Home:
    """bulb-a and plug-a monitored, Home Assistant reporting no state, no heartbeat."""
    clock = Clock()
    store = Store(tmp_path / "commands.sqlite")
    scripted = ScriptedController()
    reader = FakeReader(CATALOG)
    commands = make_commands(
        store,
        scripted if controller else None,
        reader,
        clock,
        queue=queue,
        slots=slots,
        retention_days=retention_days,
    )
    house = Home(store, clock, scripted, reader, commands)
    house.select(LIGHT)
    house.select(PLUG)
    return house


def ready(
    tmp_path: Path,
    device: Device,
    state: Payload = IS_OFF,
    *,
    queue: float = 3.0,
    slots: int = 4,
    retention_days: int = 30,
) -> Home:
    """A home with control on and Home Assistant reporting `device` in `state`."""
    house = home(tmp_path, queue=queue, slots=slots, retention_days=retention_days)
    house.report(device, state)
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
    house.report(LIGHT, IS_OFF)
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
    house.report(LIGHT, IS_OFF)
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
    house.report(LIGHT, IS_OFF)
    assert isinstance(await switch(house, entity, ON), expected)
    assert house.controller.sent == []
    assert house.audit() == []


def refusal_cases() -> list[object]:
    """(device, action, reported state, monitored, heartbeat, refusal code), in check order."""
    shared: list[tuple[str, Payload, Payload | None, bool, bool, CommandRefusalCode]] = [
        ("control off", ON, IS_OFF, True, False, "control_off"),
        # Control is checked before monitoring.
        ("control off and not monitored", ON, None, False, False, "control_off"),
        ("not monitored", ON, None, False, True, "not_monitored"),
        # Home Assistant has no state for the device.
        ("no state", ON, None, True, True, "state_unknown"),
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
    ("device", "action", "state", "monitored", "heartbeat", "code"), refusal_cases()
)
async def test_refusals_come_in_a_fixed_order_and_nothing_is_sent_or_stored(
    tmp_path: Path,
    *,
    device: Device,
    action: Payload,
    state: Payload | None,
    monitored: bool,
    heartbeat: bool,
    code: CommandRefusalCode,
) -> None:
    house = home(tmp_path)
    if state is not None:
        house.report(device, state)
    if not monitored:
        house.select(device, selected=False)
    if heartbeat:
        assert house.commands.heartbeat(NOW)
    assert await switch(house, device, action) == refused(code, device)
    assert house.controller.sent == []
    assert house.audit() == []


@pytest.mark.parametrize("device", DEVICES)
async def test_unreachable_home_assistant_makes_the_state_unknown_and_nothing_is_sent(
    tmp_path: Path, device: Device
) -> None:
    house = ready(tmp_path, device)
    house.reader.error = "connection"
    assert await switch(house, device, ON) == refused("state_unknown", device)
    assert house.controller.sent == []
    assert house.audit() == []


async def test_brightness_is_refused_for_a_switch_reading_even_when_the_catalog_says_light(
    tmp_path: Path,
) -> None:
    house = home(tmp_path)
    # The catalog lists bulb-a as a light; Home Assistant now reports it as a switch.
    house.report(Device("bulb-a", "switch.desk", "switch", "device"), IS_OFF)
    assert house.commands.heartbeat(NOW)
    assert await switch(house, LIGHT, on(30)) == refused("not_supported")
    assert house.controller.sent == []


async def test_state_from_outside_the_device_domain_makes_the_entity_unsupported(
    tmp_path: Path,
) -> None:
    # The controller only takes a light's own `light.*` entity ID.
    house = home(tmp_path)
    house.report(LIGHT, IS_OFF, source="switch.desk")
    assert house.commands.heartbeat(NOW)
    assert isinstance(await switch(house, LIGHT, ON), UnsupportedEntity)
    assert house.controller.sent == []
    assert house.audit() == []


# Forwarding


@pytest.mark.parametrize(
    ("device", "state", "action"),
    [
        pytest.param(LIGHT, IS_OFF, ON, id="light on"),
        pytest.param(LIGHT, IS_OFF, on(30), id="light on at 30 %"),
        pytest.param(LIGHT, IS_ON, OFF, id="light off"),
        # A different brightness is a change: 74 is three steps from 77.
        pytest.param(LIGHT, {"state": "on", "brightness": 74}, on(30), id="light dim to 30 %"),
        pytest.param(LIGHT, {"state": "on", "brightness": 100}, on(30), id="light from full"),
        # A light with no brightness cannot already be at 30 %.
        pytest.param(LIGHT, {"state": "on", "color_mode": "onoff"}, on(30), id="light no level"),
        pytest.param(PLUG, IS_OFF, ON, id="plug on"),
        pytest.param(PLUG, IS_ON, OFF, id="plug off"),
    ],
)
async def test_change_is_forwarded_to_the_source_entity_home_assistant_reports(
    tmp_path: Path, device: Device, state: Payload, action: Payload
) -> None:
    house = ready(tmp_path, device, state)
    record = recorded(await switch(house, device, action))
    assert house.controller.bodies() == [forwarded(device, action)]
    assert record.device_kind == device.kind


@pytest.mark.parametrize("device", DEVICES)
async def test_forwarding_follows_a_renamed_home_assistant_entity(
    tmp_path: Path, device: Device
) -> None:
    house = ready(tmp_path, device)
    # Home Assistant now attributes the device to a renamed entity.
    renamed = f"{device.source}_2"
    house.report(device, IS_OFF, source=renamed)
    recorded(await switch(house, device, ON))
    assert house.controller.bodies() == [forwarded(device, ON, renamed)]


# Confirming the result by reading the device's state after the send


@pytest.mark.parametrize(
    ("device", "action", "shown"),
    [
        pytest.param(LIGHT, on(30), {"state": "on", "brightness": 77}, id="light"),
        pytest.param(PLUG, ON, IS_ON, id="plug"),
    ],
)
async def test_command_is_observed_when_home_assistant_shows_the_commanded_state(
    tmp_path: Path, device: Device, action: Payload, shown: Payload
) -> None:
    house = ready(tmp_path, device)
    house.controller.steps.append(lambda: house.report(device, shown))
    record = recorded(await switch(house, device, action, TELEGRAM))
    # No event ID: the state read is the evidence, and a record names no event.
    assert record.model_dump(mode="json", exclude={"id", "sent_at", "command"}) == {
        "entity_id": device.entity_id,
        "device_kind": device.kind,
        "sender": TELEGRAM,
        "outcome": "observed",
    }
    assert record.command.model_dump(exclude_none=True) == action
    # The send time is taken before the controller is called.
    assert datetime.fromisoformat(record.sent_at) == NOW
    assert house.controller.bodies() == [forwarded(device, action)]
    assert house.audit() == [record]


@pytest.mark.parametrize("device", DEVICES)
async def test_off_command_is_observed_from_an_off_state(tmp_path: Path, device: Device) -> None:
    house = ready(tmp_path, device, IS_ON)
    house.controller.steps.append(lambda: house.report(device, IS_OFF))
    assert recorded(await switch(house, device, OFF, BROWSER_CHAT)).outcome == "observed"


async def test_reads_every_half_second_and_stops_at_the_five_second_deadline(
    tmp_path: Path,
) -> None:
    house = ready(tmp_path, LIGHT)
    record = recorded(await switch(house, LIGHT, ON))
    assert record.outcome == "not_observed"
    assert "event_id" not in record.model_dump()

    reads = house.confirmation_reads
    assert reads, "no state was read after the send"
    assert reads[0] <= POLL_SECONDS
    # Today's loop polled every 0.1 s; the reader is asked every 0.5 s.
    gaps = [round(later - earlier, 6) for earlier, later in pairwise(reads)]
    assert gaps == [POLL_SECONDS] * (len(reads) - 1)
    assert WAIT_SECONDS - POLL_SECONDS <= reads[-1] <= WAIT_SECONDS
    assert all(seconds <= POLL_SECONDS for seconds in house.clock.sleeps)
    # Nothing waits past the deadline.
    assert house.sent_at is not None
    assert house.clock.now - house.sent_at <= timedelta(seconds=WAIT_SECONDS)
    assert house.audit() == [record]


async def test_reading_stops_at_the_first_state_that_shows_the_command(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT)
    house.report_from(2.0, LIGHT, IS_ON)
    record = recorded(await switch(house, LIGHT, ON))
    assert record.outcome == "observed"
    assert house.confirmation_reads[-1] == 2.0
    assert house.sent_at is not None
    assert house.clock.now - house.sent_at == timedelta(seconds=2)


async def test_reading_continues_past_a_state_that_does_not_show_the_command(
    tmp_path: Path,
) -> None:
    house = ready(tmp_path, LIGHT)
    # The light ramps up: on, but not yet at the requested level.
    house.controller.steps.append(lambda: house.report(LIGHT, {"state": "on", "brightness": 20}))
    house.report_from(1.5, LIGHT, {"state": "on", "brightness": 77})
    record = recorded(await switch(house, LIGHT, on(30)))
    assert record.outcome == "observed"
    assert house.confirmation_reads[-1] == 1.5


@pytest.mark.parametrize("error", ["connection", "reader_unreachable", "timeout"])
async def test_failing_reads_after_the_send_end_as_not_observed_by_the_deadline(
    tmp_path: Path, error: Literal["connection", "reader_unreachable", "timeout"]
) -> None:
    house = ready(tmp_path, LIGHT)

    def lose_home_assistant() -> None:
        house.reader.error = error

    house.controller.steps.append(lose_home_assistant)
    record = recorded(await switch(house, LIGHT, ON))
    assert record.outcome == "not_observed"
    assert house.audit() == [record]
    assert house.sent_at is not None
    assert house.clock.now - house.sent_at <= timedelta(seconds=WAIT_SECONDS)


async def test_slow_reads_end_as_not_observed_and_never_stay_pending(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT)

    def slow_read() -> None:
        # Each read takes three seconds of the clock.
        house.clock.now += timedelta(seconds=3)

    house.after_send.append(slow_read)
    record = recorded(await switch(house, LIGHT, ON))
    assert record.outcome == "not_observed"
    assert [stored.outcome for stored in house.audit()] == ["not_observed"]
    # Reads at 0 and 3 s of the send fit the deadline; none starts after it.
    assert all(seconds <= WAIT_SECONDS for seconds in house.confirmation_reads)


@pytest.mark.parametrize(
    ("brightness", "outcome"),
    [(76, "observed"), (78, "observed"), (79, "observed"), (74, "not_observed")],
)
async def test_brightness_within_two_steps_of_the_requested_percent_counts_as_observed(
    tmp_path: Path, brightness: int, outcome: str
) -> None:
    house = ready(tmp_path, LIGHT)
    shown: Payload = {"state": "on", "brightness": brightness}
    house.controller.steps.append(lambda: house.report(LIGHT, shown))
    assert recorded(await switch(house, LIGHT, on(30))).outcome == outcome


async def test_another_devices_change_does_not_count(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT)
    house.select(OTHER_LIGHT)
    house.report(OTHER_LIGHT, IS_OFF)
    house.controller.steps.append(lambda: house.report(OTHER_LIGHT, IS_ON))
    assert recorded(await switch(house, LIGHT, ON)).outcome == "not_observed"


@pytest.mark.parametrize("device", DEVICES)
async def test_device_that_keeps_its_state_is_not_observed(tmp_path: Path, device: Device) -> None:
    # Home Assistant took the call, but the device still reports the old state.
    house = ready(tmp_path, device)
    record = recorded(await switch(house, device, ON))
    assert record.outcome == "not_observed"
    assert house.audit() == [record]


@pytest.mark.parametrize("outcome", ["refused", "unreachable"])
@pytest.mark.parametrize("device", DEVICES)
async def test_controller_that_refuses_or_fails_is_audited_without_reading(
    tmp_path: Path, device: Device, outcome: str
) -> None:
    house = ready(tmp_path, device)
    house.controller.reply = "refused"
    house.controller.unreachable = outcome == "unreachable"
    record = recorded(await switch(house, device, ON))
    assert record.outcome == outcome
    assert house.confirmation_reads == []
    assert house.clock.sleeps == []
    assert house.controller.bodies() == [forwarded(device, ON)]
    assert house.audit() == [record]


# The stored record


async def test_command_is_stored_as_pending_while_the_controller_works(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT)
    during_send: list[list[Recorded]] = []
    house.controller.steps.append(lambda: during_send.append(house.audit()))
    record = recorded(await switch(house, LIGHT, on(30), TELEGRAM))
    assert during_send == [[record.model_copy(update={"outcome": "pending"})]]
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

    restarted = make_commands(house.store, ScriptedController(), house.reader, house.clock)
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


async def test_device_that_stops_being_monitored_during_the_last_state_read_is_not_sent(
    tmp_path: Path,
) -> None:
    house = ready(tmp_path, LIGHT)
    reads = 0

    def stop_monitoring_during_the_second_check() -> None:
        # The second check runs with the send slot held and is the last before the send.
        nonlocal reads
        reads += 1
        if reads == 2:
            house.select(LIGHT, selected=False)

    house.reader.on_read = stop_monitoring_during_the_second_check
    assert await switch(house, LIGHT, ON) == refused("not_monitored")
    assert house.controller.sent == []
    assert house.audit() == []


# The command deadline

# Below the agent's 10-second request timeout.
DEADLINE_SECONDS = 8.0


def slow_catalog(house: Home, *seconds: float) -> None:
    """Each catalog read takes the next of `seconds` on the test clock."""
    delays = list(seconds)
    catalog = house.reader.catalog

    def slow() -> list[Entity]:
        house.clock.now += timedelta(seconds=delays.pop(0) if delays else 0)
        return catalog()

    house.reader.catalog = slow  # pyright: ignore[reportAttributeAccessIssue] -- a slow Home Assistant


@pytest.mark.parametrize(
    "seconds",
    [
        pytest.param((DEADLINE_SECONDS + 1,), id="first check"),
        # Each read is well inside the reader's timeout; together they pass the deadline.
        pytest.param((DEADLINE_SECONDS / 2 + 1, DEADLINE_SECONDS / 2 + 1), id="second check"),
    ],
)
async def test_command_whose_checks_outlast_the_deadline_is_refused_as_busy(
    tmp_path: Path, seconds: tuple[float, ...]
) -> None:
    house = ready(tmp_path, LIGHT)
    slow_catalog(house, *seconds)
    assert await switch(house, LIGHT, ON) == refused("busy")
    assert house.controller.sent == []
    assert house.audit() == []


async def test_waiting_for_a_send_slot_ends_at_the_deadline(tmp_path: Path) -> None:
    house = ready(tmp_path, LIGHT, slots=1, queue=5)
    entered, release = hold_the_only_slot(house)
    first: list[Result] = []

    async def command() -> None:
        first.append(await switch(house, LIGHT, ON))

    async with anyio.create_task_group() as group:
        group.start_soon(command)
        await anyio.to_thread.run_sync(entered.wait, 5)
        # The queued command's first check leaves it a quarter second before the deadline.
        slow_catalog(house, DEADLINE_SECONDS - 0.25)
        started = anyio.current_time()
        try:
            queued = await switch(house, LIGHT, ON)
        finally:
            release.set()
        waited = anyio.current_time() - started
    assert queued == refused("busy")
    assert waited < 2, f"waited {waited:.1f} s for the slot, past the deadline"
    assert len(house.controller.sent) == 1


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
    house.report(PLUG, IS_OFF)
    house.controller.reply = "refused"
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
        house.clock.now += timedelta(seconds=10)
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
