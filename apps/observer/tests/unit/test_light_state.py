"""Normalizing raw Home Assistant light state into the observer's light model."""

from home_observer.events import light_state


def test_raw_fields_never_leave_normalizer() -> None:
    state = light_state(
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
    assert state is not None
    assert state.brightness == 42
    assert state.hs_color == (10, 20)
    encoded = state.model_dump_json()
    for sentinel in ("ignore approvals", "private-account", "secret-marker", "<script>"):
        assert sentinel not in encoded
