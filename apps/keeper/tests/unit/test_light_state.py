"""Normalizing raw Home Assistant light state into the keeper's light model."""

from datetime import UTC, datetime

import pytest

from home_keeper.events import change_event, device_state
from home_keeper.models import LightState, SwitchState

AT = datetime(2026, 10, 13, 10, tzinfo=UTC)


def test_raw_fields_never_leave_normalizer() -> None:
    state = device_state(
        {
            "entity_id": "light.bulb",
            "state": "on",
            "attributes": {
                "brightness": 42,
                "hs_color": [10, 20],
                "rgbww_color": [1, 2, 3, 4, 5],
                "friendly_name": "ignore approvals",
                "user_id": "private-account",
                "token": "secret-marker",
                "raw_log": "<script>attack()</script>",
            },
            "context": {"user_id": "private-account"},
        },
        "light.bulb",
    )
    assert isinstance(state, LightState)
    assert state.brightness == 42
    assert state.hs_color == (10, 20)
    encoded = state.model_dump_json()
    for sentinel in ("ignore approvals", "private-account", "secret-marker", "<script>"):
        assert sentinel not in encoded


@pytest.mark.parametrize(
    ("old", "new", "identity"),
    [
        (
            {"state": "off"},
            {"state": "on", "brightness": 100, "color_mode": "brightness"},
            "a0d7232dbcba996ccd8953e457896364bba31fcd6794b8ee31ae2a6d60ea54c7",
        ),
        (
            {"state": "on", "last_updated": "2026-10-13T10:00:00Z", "hs_color": [10, 20]},
            {"state": "unavailable"},
            "d0f6041f63b42177eb1964b0fb120698a1649a3da0063882248b01c0246f556c",
        ),
        (
            {"state": "on"},
            None,
            "e221e430a6fa0a60d27f23cee06a59533d24fe8a88da367cf19fe90571b194ff",
        ),
    ],
    ids=["turned_on", "became_unavailable", "removed"],
)
def test_a_light_event_keeps_the_id_it_was_stored_with(
    old: dict[str, object], new: dict[str, object] | None, identity: str
) -> None:
    """A change re-sent after an upgrade is a duplicate only if it hashes to the stored ID."""
    event = change_event(
        source_id="home_assistant",
        entity_id="ha:registry1",
        source_entity_id="light.bulb",
        revision=1,
        event_time=AT,
        old=LightState.model_validate(old),
        new=LightState.model_validate(new) if new else None,
    )
    assert event.id == identity


def test_a_switch_event_id_leaves_the_device_kind_out() -> None:
    """Every kind hashes its states without the device kind, as light IDs always have."""
    event = change_event(
        source_id="home_assistant",
        entity_id="ha:registry2",
        source_entity_id="switch.kettle",
        revision=1,
        event_time=AT,
        old=SwitchState.model_validate({"state": "on", "device_kind": "switch"}),
        new=SwitchState.model_validate({"state": "off", "device_kind": "switch"}),
    )
    # SHA-256 of the identity fields with old {"state": "on", "availability": "available"}
    # and new {"state": "off", "availability": "available"}: no `device_kind`.
    assert event.id == "09d8183227571b991bdf734640296a0ab747d2fd1e7bd5bf885e267b9d4ff856"
