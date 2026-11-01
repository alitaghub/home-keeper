"""The synthetic home the demo reader serves. No device, vendor, network-administration or
model access.

With the `control` profile, the demo controller's commands switch the synthetic lights and
plug. No command switches a device of any other kind.
"""

import math
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TypedDict, overload

from home_keeper.models import (
    Bucket,
    DeviceKind,
    DeviceState,
    GenericState,
    LightCommand,
    LightState,
    MeasurementState,
    OnOffState,
    SwitchState,
    statistics_period,
)
from home_keeper_light_command import (
    BRIGHTNESS_MAX,
    LightOff,
    LightOn,
    SwitchCommand,
    SwitchOff,
    SwitchOn,
    brightness_value,
)

# A switched light keeps its commanded state for this many replay passes.
HELD_PASSES = 3
# The words Home Assistant's English translations give the demo's states.
DEMO_LABELS = {"on": "On", "off": "Off", "open": "Open", "closed": "Closed"}
# Devices whose words depend on their device class, as Home Assistant's translations do: a
# door sensor's on and off are "Open" and "Closed".
DEVICE_CLASS_LABELS = {"binary_sensor.front_door": {"on": "Open", "off": "Closed"}}
# The garage door's state for each replay step's on, off or unavailable.
DOOR_STATES = {"on": "open", "off": "closed", "unavailable": "unavailable"}
# The desk plug's power, in watts, for each replay step's on, off or unavailable.
POWER_STATES = {"on": "412", "off": "0", "unavailable": "unavailable"}
POWER_UNIT = "W"
# How far back the synthetic statistics reach.
DEMO_STATISTICS = timedelta(days=30)


def demo_label(state: str, identity: str | None = None) -> str:
    """A demo state's translated word, else the state itself, as for Home Assistant."""
    words = DEVICE_CLASS_LABELS.get(identity or "", {})
    return words.get(state) or DEMO_LABELS.get(state, state)


class DemoEntity(TypedDict):
    id: str
    device_id: str | None
    device_name: str | None
    name: str
    device_kind: DeviceKind | None
    supported: bool
    reason: str | None


ENTITIES: list[DemoEntity] = [
    {
        "id": "light.hue_desk",
        "device_id": "hue-desk",
        "device_name": "Desk lamp",
        "name": "Desk light",
        "device_kind": "light",
        "supported": True,
        "reason": None,
    },
    {
        "id": "light.hue_reading",
        "device_id": "hue-reading",
        "device_name": "Reading lamp",
        "name": "Reading light <script>alert('untrusted')</script>",
        "device_kind": "light",
        "supported": True,
        "reason": None,
    },
    {
        "id": "light.hue_room",
        "device_id": "hue-room",
        "device_name": "Living room group",
        "name": "All living room lights",
        "device_kind": "light",
        "supported": False,
        "reason": "Groups and rooms are not recorded",
    },
    {
        "id": "switch.smart_plug",
        "device_id": "smart-plug",
        "device_name": "Smart plug",
        "name": "Plug",
        "device_kind": "switch",
        "supported": True,
        "reason": None,
    },
    {
        "id": "cover.garage_door",
        "device_id": "garage-door",
        "device_name": "Garage door",
        "name": "Door",
        "device_kind": "state",
        "supported": True,
        "reason": None,
    },
    {
        "id": "fan.ceiling_fan",
        "device_id": "ceiling-fan",
        "device_name": "Ceiling fan",
        "name": "Fan",
        "device_kind": "on_off",
        "supported": True,
        "reason": None,
    },
    {
        "id": "binary_sensor.front_door",
        "device_id": "front-door",
        "device_name": "Front door",
        "name": "Door",
        "device_kind": "on_off",
        "supported": True,
        "reason": None,
    },
    {
        "id": "sensor.desk_plug_power",
        "device_id": "smart-plug",
        "device_name": "Smart plug",
        "name": "Desk plug power",
        "device_kind": "measurement",
        "supported": True,
        "reason": None,
    },
    {
        "id": "sensor.ungrouped",
        "device_id": None,
        "device_name": None,
        "name": "Ungrouped sensor",
        "device_kind": "state",
        "supported": False,
        "reason": "Only entities of a device are recorded",
    },
]


class DemoReplay:
    """Repeatable synthetic transitions."""

    def __init__(self) -> None:
        self.phase = 0
        # Commands and replay passes change the same states from two threads.
        self.lock = Lock()
        # Lights a command switched, with the replay passes they still skip.
        self.held: dict[str, int] = {}
        # Each light's brightness from when it was last on, which an off state does not carry.
        self.last_brightness: dict[str, int] = {}
        since = {"last_changed": "2025-01-01T12:00:00Z", "last_updated": "2025-01-01T12:00:00Z"}
        self.states: dict[str, DeviceState] = {
            entity["id"]: self._initial(entity["id"], entity["device_kind"], since)
            for entity in ENTITIES
            if entity["supported"]
        }

    @staticmethod
    def _initial(identity: str, kind: DeviceKind | None, since: dict[str, str]) -> DeviceState:
        if kind == "switch":
            return SwitchState.model_validate(
                since | {"state": "on", "device_kind": "switch", "label": demo_label("on")}
            )
        if kind == "on_off":
            return OnOffState.model_validate(
                since
                | {"state": "on", "device_kind": "on_off", "label": demo_label("on", identity)}
            )
        if kind == "state":
            return GenericState.model_validate(
                since | {"state": "open", "device_kind": "state", "label": demo_label("open")}
            )
        if kind == "measurement":
            return MeasurementState.model_validate(
                since
                | {
                    "state": POWER_STATES["on"],
                    "device_kind": "measurement",
                    "unit": POWER_UNIT,
                    "label": demo_label(POWER_STATES["on"]),
                }
            )
        brightness = 180 if identity == "light.hue_desk" else 100
        return LightState.model_validate(
            since | {"state": "on", "brightness": brightness, "label": demo_label("on")}
        )

    def advance(self, event_time: datetime) -> dict[str, DeviceState]:
        sequence: tuple[dict[str, object], ...] = (
            {"state": "off"},
            {"state": "on", "brightness": 100},
            {"state": "on", "brightness": 40},
            {"state": "on", "color_mode": "color_temp", "color_temp_kelvin": 2700},
            {"state": "on", "color_mode": "xy", "xy_color": (0.4, 0.3)},
            {"state": "unavailable"},
            {"state": "on", "brightness": 180, "color_mode": "brightness"},
        )
        value = sequence[self.phase % len(sequence)]
        self.phase += 1
        previous = self.states
        self.last_brightness |= {
            identity: old.brightness
            for identity, old in previous.items()
            if isinstance(old, LightState) and old.brightness is not None
        }
        self.states = {
            identity: old if identity in self.held else self._next(identity, old, value, event_time)
            for identity, old in previous.items()
        }
        self.held = {identity: passes - 1 for identity, passes in self.held.items() if passes > 1}
        return previous

    @staticmethod
    def _next[State: DeviceState](
        identity: str, old: State, value: dict[str, object], event_time: datetime
    ) -> State:
        times = {
            "last_changed": event_time if old.state != value["state"] else old.last_changed,
            "last_updated": event_time,
            "last_reported": event_time,
            "label": demo_label(str(value["state"]), identity),
        }
        if isinstance(old, SwitchState | OnOffState):
            # A plug, the fan and the door sensor follow the lights' on, off and unavailable
            # steps, without their attributes.
            return old.model_validate(
                {"state": value["state"], "device_kind": old.device_kind} | times
            )
        if isinstance(old, MeasurementState):
            # The desk plug draws power with the lights' on and off steps.
            state = POWER_STATES.get(str(value["state"]), old.state)
            times["last_changed"] = event_time if old.state != state else old.last_changed
            times["label"] = demo_label(state)
            return old.model_validate(
                {"state": state, "device_kind": "measurement", "unit": old.unit} | times
            )
        if isinstance(old, GenericState):
            # The garage door opens and closes with the lights' on and off steps.
            state = DOOR_STATES.get(str(value["state"]), old.state)
            times["last_changed"] = event_time if old.state != state else old.last_changed
            times["label"] = demo_label(state)
            return old.model_validate({"state": state, "device_kind": "state"} | times)
        return old.model_validate(value | times)

    @overload
    def switch(
        self, identity: str, command: LightCommand, event_time: datetime
    ) -> tuple[LightState, LightState] | None: ...
    @overload
    def switch(
        self, identity: str, command: SwitchCommand, event_time: datetime
    ) -> tuple[SwitchState, SwitchState] | None: ...
    def switch(
        self, identity: str, command: LightCommand | SwitchCommand, event_time: datetime
    ) -> tuple[LightState, LightState] | tuple[SwitchState, SwitchState] | None:
        """Apply a command and hold the device; returns (old, new), or None if it cannot switch."""
        old = self.states.get(identity)
        # No command switches a generic, other on/off or measurement device.
        if (
            old is None
            or isinstance(old, GenericState | OnOffState | MeasurementState)
            or old.state not in ("on", "off")
        ):
            return None
        if isinstance(old, SwitchState):
            if not isinstance(command, SwitchOn | SwitchOff):
                return None
            switched = self._next(identity, old, {"state": command.action}, event_time)
            self.states[identity] = switched
            self.held[identity] = HELD_PASSES
            return old, switched
        if not isinstance(command, LightOn | LightOff):
            return None
        if old.brightness is not None:
            self.last_brightness[identity] = old.brightness
        if isinstance(command, LightOff):
            value: dict[str, object] = {"state": "off"}
        else:
            # Like Home Assistant, turning on without a brightness keeps the last one.
            brightness = (
                brightness_value(command.brightness_pct)
                if command.brightness_pct is not None
                else self.last_brightness.get(identity, BRIGHTNESS_MAX)
            )
            value = {"state": "on", "brightness": brightness, "color_mode": "brightness"}
        new = self._next(identity, old, value, event_time)
        self.states[identity] = new
        self.held[identity] = HELD_PASSES
        return old, new


def demo_power(moment: datetime) -> float:
    """The desk plug's synthetic mean power, in watts, for the period starting at `moment`:
    a daily curve with a repeatable wobble, so every read gives the same numbers."""
    seconds = moment.timestamp()
    daily = math.sin(2 * math.pi * (seconds % 86400) / 86400)
    wobble = (int(seconds // 300) * 2654435761 % 1000) / 1000
    return round(250 + 150 * daily + 40 * wobble, 1)


def demo_statistics(start: datetime, end: datetime, now: datetime) -> list[Bucket]:
    """Synthetic statistics like Home Assistant's: every whole period that starts in
    `[start, end)`, has ended by `now` and starts within DEMO_STATISTICS of it, oldest first.
    The period follows the range, as for Home Assistant."""
    period = statistics_period(start, end)
    step = timedelta(minutes=5) if period == "5minute" else timedelta(hours=1)
    epoch = datetime.fromtimestamp(0, UTC)
    # Periods start at whole multiples of their length, as Home Assistant's do.
    moment = epoch + math.ceil((max(start, now - DEMO_STATISTICS) - epoch) / step) * step
    buckets: list[Bucket] = []
    while moment < end and moment + step <= now:
        mean = demo_power(moment)
        buckets.append(
            Bucket(
                start=moment.isoformat(),
                end=(moment + step).isoformat(),
                mean=mean,
                min=round(mean * 0.8, 1),
                max=round(mean * 1.25, 1),
            )
        )
        moment += step
    return buckets
