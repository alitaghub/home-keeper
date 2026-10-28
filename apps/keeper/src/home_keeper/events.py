"""Minimal, attributed device changes shared by trusted collectors."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, TypeIs

from home_keeper.models import ChangeEvent, LightState, SwitchState, device_kind_of

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


def device_state(payload: object, source_entity_id: str) -> LightState | SwitchState | None:
    """A light or switch state, by the entity's domain. A switch keeps no light attributes."""
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
    if device_kind_of(source_entity_id) == "switch":
        return SwitchState.model_validate(state | {"device_kind": "switch"})
    return LightState.model_validate(
        state | {key: attributes[key] for key in COLOR_FIELDS if attributes.get(key) is not None}
    )


def change_event(
    *,
    source_id: str,
    entity_id: str,
    source_entity_id: str,
    revision: int,
    event_time: datetime,
    old: LightState | SwitchState | None,
    new: LightState | SwitchState | None,
    received_at: datetime | None = None,
) -> ChangeEvent:
    identity_data: dict[str, object] = {
        "source_id": source_id,
        "entity_id": entity_id,
        "source_entity_id": source_entity_id,
        "event_time": event_time.isoformat(),
        "old": old.stored() if old else None,
        "new": new.stored() if new else None,
    }
    event_id = hashlib.sha256(
        json.dumps(identity_data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ChangeEvent.model_validate(
        {
            "id": event_id,
            "selection_revision": revision,
            "received_at": received_at or datetime.now(UTC),
            **identity_data,
        }
    )
