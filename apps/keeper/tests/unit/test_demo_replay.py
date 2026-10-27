from datetime import UTC, datetime

from home_keeper.demo import DemoReplay
from home_keeper_light_command import LightOff, LightOn

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def test_demo_light_turned_off_then_on_keeps_its_last_brightness() -> None:
    replay = DemoReplay()
    before = replay.states["light.hue_desk"].brightness
    assert replay.switch("light.hue_desk", LightOff(action="off"), NOW)
    change = replay.switch("light.hue_desk", LightOn(action="on"), NOW)
    assert change is not None
    assert change[1].brightness == before


def test_demo_light_the_replay_turned_off_turns_on_at_its_last_brightness() -> None:
    replay = DemoReplay()
    before = replay.states["light.hue_desk"].brightness
    replay.advance(NOW)  # The replay's first pass turns every light off.
    assert replay.states["light.hue_desk"].state == "off"
    change = replay.switch("light.hue_desk", LightOn(action="on"), NOW)
    assert change is not None
    assert change[1].brightness == before
