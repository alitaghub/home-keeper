"""Compressed history rows exactly as Home Assistant 2026.10.1's recorder returned them."""

from datetime import UTC, datetime

from home_keeper.home_assistant import history_state
from home_keeper.models import LightState

# Captured from `history/history_during_period` on the bundled Home Assistant: `lu` is
# always sent, and `lc` only when it differs from `lu`.
RECORDED = [
    {
        "s": "on",
        "a": {
            "brightness": 120,
            "color_mode": "brightness",
            "supported_color_modes": ["brightness"],
        },
        "lu": 1793303017.4513721,
    },
    {
        "s": "on",
        "a": {
            "brightness": 200,
            "color_mode": "brightness",
            "supported_color_modes": ["brightness"],
        },
        "lu": 1793303018.0628774,
        "lc": 1793303017.451372,
    },
    {
        "s": "off",
        "a": {"color_mode": None, "supported_color_modes": ["brightness"]},
        "lu": 1793303018.6871755,
    },
]


def at(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


def test_recorded_rows_without_last_changed_take_it_from_last_updated() -> None:
    first, attribute_change, off = (history_state(row, "light.diag") for row in RECORDED)
    assert isinstance(first, LightState)
    assert isinstance(attribute_change, LightState)

    assert (first.state, first.brightness) == ("on", 120)
    assert first.last_changed == first.last_updated == at(1793303017.4513721)
    assert (attribute_change.brightness, attribute_change.last_updated) == (
        200,
        at(1793303018.0628774),
    )
    assert attribute_change.last_changed == at(1793303017.451372)
    assert off.state == "off"
    assert off.last_changed == off.last_updated == at(1793303018.6871755)
