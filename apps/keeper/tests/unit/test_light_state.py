"""Normalizing raw Home Assistant light state into the keeper's light model."""

from datetime import UTC, datetime

import pytest
from support import previous_release

from home_keeper.events import change_event, device_state
from home_keeper.models import LightState


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
    ("old", "new"),
    [
        ({"state": "off"}, {"state": "on", "brightness": 100, "color_mode": "brightness"}),
        (
            {"state": "on", "last_updated": "2026-10-13T10:00:00Z", "hs_color": [10, 20]},
            {"state": "unavailable"},
        ),
        ({"state": "on"}, None),
    ],
    ids=["turned_on", "became_unavailable", "removed"],
)
def test_a_light_event_keeps_the_id_the_light_only_release_gave_it(
    old: dict[str, object], new: dict[str, object] | None
) -> None:
    """Duplicate detection across an upgrade depends on the same change hashing the same."""
    at = datetime(2026, 10, 13, 10, tzinfo=UTC)
    current = change_event(
        source_id="home_assistant",
        entity_id="ha:registry1",
        source_entity_id="light.bulb",
        revision=1,
        event_time=at,
        old=LightState.model_validate(old),
        new=LightState.model_validate(new) if new else None,
    )
    previous = previous_release.change_event(
        source_id="home_assistant",
        entity_id="ha:registry1",
        source_entity_id="light.bulb",
        revision=1,
        event_time=at,
        old=previous_release.LightState.model_validate(old),
        new=previous_release.LightState.model_validate(new) if new else None,
    )
    assert current.id == previous.id
