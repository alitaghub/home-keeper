import logging

import pytest

from home_observer_agent import telegram
from home_observer_agent.telegram import PollError, Settings


def test_worker_caps_backoff_resets_after_success_and_stops_on_fatal_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    delays: list[float] = []
    outcomes = iter(
        [
            *[PollError("Unavailable") for _ in range(7)],
            None,
            PollError("Unavailable"),
            PollError("Token rejected", fatal=True),
        ]
    )
    calls: list[str] = []

    class FakeBot:
        def __init__(self, settings: Settings) -> None:
            assert settings.allowed_user_ids == frozenset({11})

        def validate_startup(self) -> None:
            calls.append("startup")

        def poll_once(self) -> None:
            calls.append("poll")
            outcome = next(outcomes)
            if outcome is not None:
                raise outcome

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "11")
    monkeypatch.setattr(telegram, "TelegramBot", FakeBot)
    monkeypatch.setattr("time.sleep", delays.append)
    with caplog.at_level(logging.WARNING), pytest.raises(SystemExit, match="1"):
        telegram.main()
    assert delays == [1, 2, 4, 8, 16, 30, 30, 1]
    assert calls == ["startup", *["poll"] * 10, "close"]
    assert "Token rejected" in caplog.text
    assert "123:secret" not in caplog.text
