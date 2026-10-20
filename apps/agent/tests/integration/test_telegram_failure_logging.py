import json
import logging

import httpx
import pytest
from pydantic import SecretStr
from telegram_support import FakeTelegram, incoming

from home_observer_agent.telegram import UNCERTAIN, Settings, TelegramBot


@pytest.mark.parametrize("failure", ["http_503", "invalid_state"])
def test_update_failure_leaves_an_operator_diagnostic(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    telegram = FakeTelegram()
    agent_paths: list[str] = []
    offsets: list[int] = []

    def telegram_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            offsets.append(json.loads(request.content)["offset"])
            return httpx.Response(
                200, json={"ok": True, "result": [incoming(1, 11, "Private question")]}
            )
        return telegram.handle(request)

    def agent_transport(request: httpx.Request) -> httpx.Response:
        agent_paths.append(request.url.path)
        if request.method == "POST" and request.url.path == "/threads":
            return httpx.Response(200, json={})
        assert request.method == "GET"
        assert request.url.path.endswith("/state")
        if failure == "http_503":
            return httpx.Response(503, json={"detail": "Private backend detail"})
        return httpx.Response(200, json={"values": {"messages": "Private invalid data"}})

    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:private-token"),
            allowed_user_ids=frozenset({11}),
            state_path=":memory:",
        ),
        telegram_client=httpx.Client(transport=httpx.MockTransport(telegram_transport)),
        agent_client=httpx.Client(transport=httpx.MockTransport(agent_transport)),
    )
    try:
        with caplog.at_level(logging.WARNING):
            bot.poll_once()
        assert len(agent_paths) == 2
        assert telegram.bodies()[-1]["text"] == UNCERTAIN
        bot.poll_once()
        assert offsets == [0, 2]
    finally:
        bot.close()

    assert "private-token" not in caplog.text
    assert "Private" not in caplog.text
    assert caplog.records, (
        f"{failure}: update was acknowledged and user got only UNCERTAIN, "
        "but no operator warning/error was logged"
    )
