import logging
from pathlib import Path

import pytest
from agent_support import FakeObserver
from langchain_core.messages import AIMessage
from pydantic import SecretStr
from telegram_support import FakeTelegram, GraphAegra, incoming

from home_observer_agent.telegram import UNCERTAIN, Settings, TelegramBot


def test_new_recovers_from_invalid_saved_conversation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Recovered answer.")], FakeObserver())
    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    # Seed a corrupted external state file; assertions exercise the bot's public update boundary.
    invalid = '{"thread_id":"private-old-thread","status":"pending"}'
    try:
        bot.db.execute("INSERT INTO conversations VALUES (?, ?)", (11, invalid))
        bot.db.commit()
        with caplog.at_level(logging.WARNING):
            bot.handle_update(incoming(1, 11, "Private question"))
        assert agent.requests == []
        assert telegram.bodies()[-1]["text"] == UNCERTAIN
        assert "operation=message exception=ValidationError status=None" in caplog.text
        assert "private-old-thread" not in caplog.text
        assert "Private question" not in caplog.text
        bot.handle_update(incoming(2, 11, "/new"))
        bot.handle_update(incoming(3, 11, "A new question"))
        assert [body["text"] for body in telegram.bodies()] == [
            UNCERTAIN,
            "Started a fresh conversation. Old history is kept.",
            "Recovered answer.",
        ]
        assert len(agent.submissions()) == 1
    finally:
        bot.close()
