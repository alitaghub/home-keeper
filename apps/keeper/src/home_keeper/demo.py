"""The synthetic home the demo reader serves. No device, vendor, network-administration or
model access.

With the `control` profile, the demo controller forwards light and switch commands to the
demo reader, and they switch the synthetic home's lights and plug.
"""

from datetime import datetime
from threading import Lock
from typing import TypedDict, overload

from home_keeper.models import (
    DeviceKind,
    LightCommand,
    LightState,
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
        "id": "sensor.ungrouped",
        "device_id": None,
        "device_name": None,
        "name": "Ungrouped sensor",
        "device_kind": None,
        "supported": False,
        "reason": "Only lights, plugs and switches are supported",
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
        self.states: dict[str, LightState | SwitchState] = {
            entity["id"]: SwitchState.model_validate(
                since | {"state": "on", "device_kind": "switch"}
            )
            if entity["device_kind"] == "switch"
            else LightState.model_validate(
                since
                | {"state": "on", "brightness": 180 if entity["id"] == "light.hue_desk" else 100}
            )
            for entity in ENTITIES
            if entity["supported"]
        }

    def advance(self, event_time: datetime) -> dict[str, LightState | SwitchState]:
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
            identity: old if identity in self.held else self._next(old, value, event_time)
            for identity, old in previous.items()
        }
        self.held = {identity: passes - 1 for identity, passes in self.held.items() if passes > 1}
        return previous

    @staticmethod
    def _next[State: LightState | SwitchState](
        old: State, value: dict[str, object], event_time: datetime
    ) -> State:
        times = {
            "last_changed": event_time if old.state != value["state"] else old.last_changed,
            "last_updated": event_time,
            "last_reported": event_time,
        }
        if isinstance(old, SwitchState):
            # A plug follows the lights' on, off and unavailable steps, without their attributes.
            return old.model_validate({"state": value["state"], "device_kind": "switch"} | times)
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
        if old is None or old.state not in ("on", "off"):
            return None
        if isinstance(old, SwitchState):
            if not isinstance(command, SwitchOn | SwitchOff):
                return None
            switched = self._next(old, {"state": command.action}, event_time)
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
        new = self._next(old, value, event_time)
        self.states[identity] = new
        self.held[identity] = HELD_PASSES
        return old, new
