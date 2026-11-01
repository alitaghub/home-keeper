"""Parses a Home Assistant state into a device's state, keeping only the approved light
attributes."""

from typing import Any, TypeIs

from home_keeper.models import DeviceState, GenericState, LightState, SwitchState, device_kind_of

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


def is_json_object(value: object) -> TypeIs[dict[str, Any]]:
    """Narrow decoded JSON to an object. Its values stay untrusted until validated."""
    return isinstance(value, dict)


def device_state(payload: object, source_entity_id: str) -> DeviceState | None:
    """A device's state, by the entity's domain. Only a light keeps attributes."""
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
    match device_kind_of(source_entity_id):
        case "switch":
            return SwitchState.model_validate(state | {"device_kind": "switch"})
        case "state":
            return GenericState.model_validate(state | {"device_kind": "state"})
        case "light":
            return LightState.model_validate(
                state
                | {key: attributes[key] for key in COLOR_FIELDS if attributes.get(key) is not None}
            )
        case None:
            raise ValueError("Not a device")
