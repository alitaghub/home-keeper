import json
import logging

import pytest
from agent_support import FakeKeeper, command_requests
from langchain_core.messages import AIMessage
from pydantic import SecretStr
from telegram_support import FakeTelegram, GraphAegra, clicked, incoming

from home_keeper_agent.telegram import UNCERTAIN, Settings, TelegramBot


def test_new_recovers_from_invalid_saved_conversation(
    database: str, caplog: pytest.LogCaptureFixture
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Recovered answer.")], FakeKeeper())
    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            database_url=database,
            database_password_file=None,
        ),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    # Seed a corrupted saved conversation row; assertions exercise the bot's public update boundary.
    invalid = '{"thread_id":"private-old-thread","status":"pending"}'
    try:
        bot.db.execute("INSERT INTO conversations VALUES (%s, %s)", (11, invalid))
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


@pytest.mark.parametrize("old_name", ["turn_on_light", "turn_off_light", "monitor_light"])
def test_saved_approval_for_a_renamed_tool_is_unusable_and_new_recovers(
    database: str, old_name: str
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper()
    agent = GraphAegra([AIMessage("Recovered answer.")], keeper)
    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            control_user_ids=frozenset({11}),
            database_url=database,
            database_password_file=None,
        ),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    # A card saved by an earlier release, whose tool names the agent no longer has.
    saved = {
        "thread_id": "old-thread",
        "status": "pending",
        "pending": {
            "token": "old",
            "interrupt_id": "old-interrupt",
            "approval": {
                "action_requests": [{"name": old_name, "args": {"entity_id": "bulb-a"}}],
                "review_configs": [
                    {"action_name": old_name, "allowed_decisions": ["approve", "reject"]}
                ],
            },
            "message_ids": [101],
            "decisions": [None],
        },
    }
    try:
        bot.db.execute("INSERT INTO conversations VALUES (%s, %s)", (11, json.dumps(saved)))

        bot.handle_update(clicked(1, 11, "old:0:approve", 101))
        bot.handle_update(incoming(2, 11, "Is it on?"))

        assert agent.submissions() == []
        assert keeper.requests == []
        assert telegram.bodies()[-1]["text"] == UNCERTAIN
        bot.handle_update(incoming(3, 11, "/new"))
        bot.handle_update(incoming(4, 11, "A new question"))
        assert telegram.bodies()[-1]["text"] == "Recovered answer."
        assert len(agent.submissions()) == 1
        assert command_requests(keeper) == []
    finally:
        bot.close()
