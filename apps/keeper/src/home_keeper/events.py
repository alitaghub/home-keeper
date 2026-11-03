"""Parses a Home Assistant state into a device's state, by the profile discovery made for it,
keeping only the approved light attributes."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeIs

from pydantic import TypeAdapter, ValidationError

from home_keeper.models import (
    DeviceState,
    GenericState,
    LightState,
    MeasurementState,
    OnOffState,
    SwitchState,
    Unit,
)

COLOR_FIELDS = (
    "brightness",
    "color_mode",
    "color_temp_kelvin",
    "hs_color",
    "xy_color",
    "rgb_color",
    "rgbw_color",
    "rgbww_color",
)
SOURCE_TIMES = ("last_changed", "last_updated", "last_reported")
_unit: TypeAdapter[Unit] = TypeAdapter(Unit)


def is_json_object(value: object) -> TypeIs[dict[str, Any]]:
    """Narrow decoded JSON to an object. Its values stay untrusted until validated."""
    return isinstance(value, dict)


def unit_of(attributes: dict[str, Any]) -> Unit | None:
    """A state's `unit_of_measurement`; None when it has none, or one too long to show."""
    try:
        return _unit.validate_python(attributes.get("unit_of_measurement"), strict=True)
    except ValidationError:
        return None


@dataclass(frozen=True)
class BasicProfile:
    """What discovery learned about a device that is not a measurement."""

    source_entity_id: str
    kind: Literal["light", "switch", "on_off", "state"]
    # Home Assistant's word for each state, by the device's class first, then its domain's.
    labels: Mapping[str, str]

    def label(self, state: str) -> str:
        return self.labels.get(state, state)


@dataclass(frozen=True)
class MeasurementProfile:
    """What discovery learned about a measurement, with the unit its readings carry."""

    source_entity_id: str
    labels: Mapping[str, str]
    unit: Unit | None
    kind: Literal["measurement"] = "measurement"

    def label(self, state: str) -> str:
        return self.labels.get(state, state)


# What discovery learned about a device: every read parses by it, so a reading's kind, unit and
# label all come from one discovery.
type DeviceProfile = BasicProfile | MeasurementProfile


def device_state(payload: object, profile: DeviceProfile) -> DeviceState | None:
    """A device's state, of the profile's kind and with its label. Only a light keeps
    attributes; a measurement carries the profile's unit."""
    if payload is None:
        return None
    if not is_json_object(payload) or payload.get("entity_id") != profile.source_entity_id:
        raise ValueError("Invalid state identity")
    attributes = payload.get("attributes", {})
    if not is_json_object(attributes):
        raise ValueError("Invalid device attributes")
    state = {
        "state": payload.get("state"),
        **{key: payload[key] for key in SOURCE_TIMES if payload.get(key) is not None},
    }
    parsed: DeviceState
    if isinstance(profile, MeasurementProfile):
        parsed = MeasurementState.model_validate(
            state | {"device_kind": "measurement", "unit": profile.unit}
        )
    else:
        match profile.kind:
            case "switch":
                parsed = SwitchState.model_validate(state | {"device_kind": "switch"})
            case "on_off":
                parsed = OnOffState.model_validate(state | {"device_kind": "on_off"})
            case "state":
                parsed = GenericState.model_validate(state | {"device_kind": "state"})
            case "light":
                parsed = LightState.model_validate(
                    state
                    | {
                        key: attributes[key]
                        for key in COLOR_FIELDS
                        if attributes.get(key) is not None
                    }
                )
    return parsed.model_copy(update={"label": profile.label(parsed.state)})
