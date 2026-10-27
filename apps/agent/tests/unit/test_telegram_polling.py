import logging
import signal

import pytest

from home_keeper_agent import telegram
from home_keeper_agent.telegram import FatalTelegramError, PollError, Settings


@pytest.mark.parametrize(
    ("retry_after", "tail_delays"),
    [(1, [1]), (45, [30, 15])],
    ids=["success-resets-backoff", "server-delay-overrides-backoff"],
)
def test_worker_caps_backoff_resets_after_success_and_stops_on_fatal_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    retry_after: int,
    tail_delays: list[int],
) -> None:
    delays: list[float] = []
    outcomes = iter(
        [
            *[PollError("Unavailable") for _ in range(7)],
            None,
            PollError("Unavailable", retry_after=retry_after),
            FatalTelegramError("Token rejected"),
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
    assert delays == [1, 2, 4, 8, 16, 30, 30, *tail_delays]
    assert calls == ["startup", *["poll"] * 10, "close"]
    assert "Token rejected" in caplog.text
    assert "123:secret" not in caplog.text


def test_sigterm_closes_worker_and_restores_previous_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []
    previous = signal.getsignal(signal.SIGTERM)

    class FakeBot:
        def __init__(self, settings: Settings) -> None:
            pass

        def validate_startup(self) -> None:
            pass

        def poll_once(self) -> None:
            signal.raise_signal(signal.SIGTERM)

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "11")
    monkeypatch.setattr(telegram, "TelegramBot", FakeBot)
    try:
        telegram.main()
        assert closed == [True]
        assert signal.getsignal(signal.SIGTERM) == previous
    finally:
        signal.signal(signal.SIGTERM, previous)
