"""The synthetic home the demo reader serves. No device, vendor, network-administration or
model access.

With the `control` profile, the demo controller forwards light and switch commands to the
demo reader, and they switch the synthetic home's lights and plug. The garage door is a
generic device: it only reports open or closed. The ceiling fan and the front door sensor are
on/off devices with usage. No command switches any of these three.
"""

from datetime import datetime
from threading import Lock
from typing import TypedDict, overload

from home_keeper.models import (
    DeviceKind,
    DeviceState,
    GenericState,
    LightCommand,
    LightState,
    OnOffState,
    SwitchState,
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
        # No command switches a generic or other on/off device.
        if (
            old is None
            or isinstance(old, GenericState | OnOffState)
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
