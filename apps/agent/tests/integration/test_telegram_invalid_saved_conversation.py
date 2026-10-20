from pathlib import Path

import httpx
from pydantic import SecretStr
from telegram_support import FakeTelegram, incoming

from home_observer_agent.telegram import Settings, TelegramBot


def test_new_recovers_from_invalid_saved_conversation(tmp_path: Path) -> None:
    telegram = FakeTelegram()
    agent_requests: list[str] = []

    def agent(request: httpx.Request) -> httpx.Response:
        agent_requests.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={})

    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=telegram.client(),
        agent_client=httpx.Client(transport=httpx.MockTransport(agent)),
    )
    invalid = '{"thread_id":"old-thread","status":"pending"}'
    try:
        bot.db.execute("INSERT INTO conversations VALUES (?, ?)", (11, invalid))
        bot.db.commit()
        bot.handle_update(incoming(1, 11, "/new"))
        bot.handle_update(incoming(2, 11, "/new"))
        replies = [body["text"] for body in telegram.bodies()]
        assert replies == ["Started a fresh conversation. Old history is kept."] * 2
        assert agent_requests == ["POST /threads"] * 2
    finally:
        bot.close()
