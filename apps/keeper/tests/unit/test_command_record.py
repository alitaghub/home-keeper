"""A command record pairs each device kind with the commands that kind accepts."""

import pytest
from pydantic import TypeAdapter, ValidationError

from home_keeper.models import CommandRecord

record: TypeAdapter[CommandRecord] = TypeAdapter(CommandRecord)
STORED: dict[str, object] = {
    "id": "command-1",
    "entity_id": "plug-a",
    "sender": {"kind": "dashboard"},
    "sent_at": "2026-10-12T10:00:00+00:00",
    "outcome": "observed",
    "event_id": None,
}


def test_a_switch_command_record_cannot_carry_a_brightness() -> None:
    switch = STORED | {"device_kind": "switch"}
    assert record.validate_python(switch | {"command": {"action": "on"}}).device_kind == "switch"
    with pytest.raises(ValidationError):
        record.validate_python(switch | {"command": {"action": "on", "brightness_pct": 40}})


def test_a_command_record_without_a_device_kind_is_a_light_command() -> None:
    light = record.validate_python(STORED | {"command": {"action": "on", "brightness_pct": 40}})
    assert light.device_kind == "light"
    assert record.dump_python(light, mode="json")["command"] == {
        "action": "on",
        "brightness_pct": 40,
    }
