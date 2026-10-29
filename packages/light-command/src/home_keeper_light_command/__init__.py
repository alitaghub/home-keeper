"""The fixed light and switch commands, parsed the same way by the API, the controller and
the demo.

No field names a domain or a service, so no command can become another Home Assistant call.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

# Home Assistant entity IDs: the device's domain and a lowercase object ID.
LightEntityId = Annotated[str, StringConstraints(pattern=r"^light\.[a-z0-9_]{1,120}$")]
SwitchEntityId = Annotated[str, StringConstraints(pattern=r"^switch\.[a-z0-9_]{1,120}$")]
BRIGHTNESS_MAX = 255
# A recorded brightness this close to the commanded value counts as the same brightness.
BRIGHTNESS_TOLERANCE = 2


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LightOn(_Wire):
    action: Literal["on"]
    brightness_pct: int | None = Field(default=None, strict=True, ge=1, le=100)


class LightOff(_Wire):
    action: Literal["off"]


LightCommand = Annotated[LightOn | LightOff, Field(discriminator="action")]


class SwitchOn(_Wire):
    """A plug or switch has no brightness."""

    action: Literal["on"]


class SwitchOff(_Wire):
    action: Literal["off"]


SwitchCommand = Annotated[SwitchOn | SwitchOff, Field(discriminator="action")]


def brightness_value(percent: int) -> int:
    """Home Assistant's 0-255 brightness for a brightness percent, rounded half up."""
    return (percent * BRIGHTNESS_MAX + 50) // 100


def matches_brightness(recorded: int | None, percent: int) -> bool:
    """Whether a recorded 0-255 brightness counts as `percent`, within the tolerance."""
    return (
        recorded is not None and abs(recorded - brightness_value(percent)) <= BRIGHTNESS_TOLERANCE
    )


class ControllerUnreachable(Exception):  # noqa: N818 -- reads as the outcome it maps to
    """No answer arrived. Never carries upstream text."""

    def __init__(self) -> None:
        super().__init__("Light controller unreachable")


class ForwardedLightCommand(_Wire):
    device_kind: Literal["light"]
    source_entity_id: LightEntityId
    command: LightCommand


class ForwardedSwitchCommand(_Wire):
    device_kind: Literal["switch"]
    source_entity_id: SwitchEntityId
    command: SwitchCommand


# The body of `/internal/command` on the controller and the demo reader. Each kind
# pairs its own entity ID pattern with its own commands, so no body mixes them.
ForwardedCommand = Annotated[
    ForwardedLightCommand | ForwardedSwitchCommand, Field(discriminator="device_kind")
]
forwarded_command: TypeAdapter[ForwardedCommand] = TypeAdapter(ForwardedCommand)


class ForwardResult(_Wire):
    """`refused`: Home Assistant answered and rejected the call."""

    result: Literal["ok", "refused"]
