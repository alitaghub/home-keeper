"""Parses a Home Assistant state into a device's state, keeping only the approved light
attributes."""

from typing import Any, TypeIs

from pydantic import TypeAdapter, ValidationError

from home_keeper.models import (
    DeviceState,
    GenericState,
    LightState,
    MeasurementState,
    OnOffState,
    SwitchState,
    Unit,
    device_kind_of,
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


def device_state(
    payload: object, source_entity_id: str, *, measured: bool = False
) -> DeviceState | None:
    """A device's state, by the entity's domain, or a measurement's when `measured`: discovery
    saw the state class `measurement` on it. Only a light keeps attributes, and a measurement
    its unit."""
    if payload is None:
        return None
    if not is_json_object(payload) or payload.get("entity_id") != source_entity_id:
        raise ValueError("Invalid state identity")
    attributes = payload.get("attributes", {})
    if not is_json_object(attributes):
        raise ValueError("Invalid device attributes")
    state = {
        "state": payload.get("state"),
        **{key: payload[key] for key in SOURCE_TIMES if payload.get(key) is not None},
    }
    match "measurement" if measured else device_kind_of(source_entity_id):
        case "measurement":
            return MeasurementState.model_validate(
                state | {"device_kind": "measurement", "unit": unit_of(attributes)}
            )
        case "switch":
            return SwitchState.model_validate(state | {"device_kind": "switch"})
        case "on_off":
            return OnOffState.model_validate(state | {"device_kind": "on_off"})
        case "state":
            return GenericState.model_validate(state | {"device_kind": "state"})
        case "light":
            return LightState.model_validate(
                state
                | {key: attributes[key] for key in COLOR_FIELDS if attributes.get(key) is not None}
            )
        case None:
            raise ValueError("Not a device")
