import json
from collections.abc import Iterator
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import Literal, Protocol

import httpx
import pytest
from agent_support import (
    FakeKeeper,
    command_requests,
    command_response,
    control_response,
    lights_response,
    snapshot,
    switch_response,
)
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import SecretStr
from telegram_support import (
    FakeTelegram,
    GraphAegra,
    TelegramBody,
    clicked,
    incoming,
    monitoring_request,
    telegram_body,
)

from home_keeper_agent.telegram import (
    REMINDER,
    STALE,
    UNCERTAIN,
    FatalTelegramError,
    PollError,
    Settings,
    TelegramBot,
    TransportError,
)
from home_keeper_agent.tools import NOT_ALLOWED as TOOL_NOT_ALLOWED


class BotFactory(Protocol):
    def __call__(
        self,
        settings: Settings,
        *,
        telegram_client: httpx.Client | None = None,
        agent_client: httpx.Client | None = None,
    ) -> TelegramBot: ...


def family_settings(
    tmp_path: Path,
    *,
    allowed_user_ids: frozenset[int] = frozenset({11}),
    control_user_ids: frozenset[int] = frozenset(),
    bot_token: SecretStr | None = None,
) -> Settings:
    return Settings(
        bot_token=bot_token or SecretStr("123:secret"),
        allowed_user_ids=allowed_user_ids,
        control_user_ids=control_user_ids,
        state_path=str(tmp_path / "bot.sqlite3"),
    )


@pytest.fixture
def make_bot() -> Iterator[BotFactory]:
    with ExitStack() as cleanup:

        def create(
            settings: Settings,
            *,
            telegram_client: httpx.Client | None = None,
            agent_client: httpx.Client | None = None,
        ) -> TelegramBot:
            bot = TelegramBot(settings, telegram_client=telegram_client, agent_client=agent_client)
            cleanup.callback(bot.close)
            return bot

        yield create


def test_only_allowed_private_messages_can_reach_the_agent(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    agent_requests: list[httpx.Request] = []
    telegram_requests: list[TelegramBody] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        telegram_requests.append(telegram_body(request))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def agent(request: httpx.Request) -> httpx.Response:
        agent_requests.append(request)
        raise AssertionError("Unauthorized messages must not reach Aegra")

    bot = make_bot(
        family_settings(tmp_path, allowed_user_ids=frozenset({11, 22})),
        telegram_client=httpx.Client(transport=httpx.MockTransport(telegram)),
        agent_client=httpx.Client(transport=httpx.MockTransport(agent)),
    )
    bot.handle_update(incoming(1, 99, "What are my lights?"))
    bot.handle_update(incoming(2, 11, "What are my lights?", kind="group"))
    bot.handle_update(incoming(3, 99, "/start"))
    bot.close()

    assert agent_requests == []
    assert len(telegram_requests) == 1
    assert telegram_requests[0]["chat_id"] == 99
    assert "99" in telegram_requests[0]["text"]


def test_family_members_keep_separate_saved_threads_and_new_preserves_old_history(
    make_bot: BotFactory,
    tmp_path: Path,
) -> None:

    telegram = FakeTelegram()
    agent = GraphAegra(
        [
            AIMessage("First answer"),
            AIMessage("Family answer"),
            AIMessage("Follow-up answer"),
            AIMessage("Fresh answer"),
        ],
        FakeKeeper(),
    )
    settings = family_settings(tmp_path, allowed_user_ids=frozenset({11, 22}))
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "First question"))
    bot.handle_update(incoming(2, 22, "Family question"))
    bot.close()
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(2, 22, "Family question"))
    bot.handle_update(incoming(3, 11, "Follow-up"))
    bot.handle_update(incoming(4, 11, "/new"))
    bot.handle_update(incoming(5, 11, "Fresh question"))
    bot.close()

    requests = agent.submissions()
    assert len(requests) == 4
    threads = [request.url.path.split("/")[2] for request in requests]
    assert threads[0] == threads[2]
    assert len({threads[0], threads[1], threads[3]}) == 3
    assert [body["text"] for body in telegram.bodies() if body["text"].endswith("answer")] == [
        "First answer",
        "Family answer",
        "Follow-up answer",
        "Fresh answer",
    ]
    # /new picks a new thread; the agent still owns the old saved history.
    old = agent.client().get(f"http://agent.test/threads/{threads[0]}/state").json()
    assert [item["content"] for item in old["values"]["messages"]] == [
        "First question",
        "First answer",
        "Follow-up",
        "Follow-up answer",
    ]


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize(
    ("name", "verb", "selected"),
    [("monitor", "Start", True), ("stop_monitoring", "Stop", False)],
)
def test_telegram_buttons_resume_real_monitoring_action_after_restart(
    make_bot: BotFactory,
    tmp_path: Path,
    decision: str,
    *,
    name: Literal["monitor", "stop_monitoring"],
    verb: str,
    selected: bool,
) -> None:

    telegram = FakeTelegram()
    keeper = FakeKeeper({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            monitoring_request(name=name, call_id="call-select"),
            AIMessage("Done."),
        ],
        keeper,
    )
    settings = family_settings(tmp_path)
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "Watch this light"))
    assert keeper.requests == []
    [prompt] = telegram.bodies()
    assert prompt["text"].splitlines()[0] == f"{verb} monitoring bulb-a?"
    buttons = prompt["reply_markup"]["inline_keyboard"][0]
    data = next(button["callback_data"] for button in buttons if button["text"].lower() == decision)
    bot.close()

    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(2, 11, "Please continue"))
    assert len(agent.submissions()) == 1
    bot.handle_update(clicked(3, 11, data, 101))
    bot.handle_update(clicked(4, 11, data, 101))
    bot.close()

    assert len(agent.submissions()) == 2
    assert telegram.bodies()[-1]["text"] == STALE
    assert "Done." in [body["text"] for body in telegram.bodies()]
    assert len(telegram.bodies("answerCallbackQuery")) == 2
    if decision == "reject":
        assert keeper.requests == []
    else:
        [write] = keeper.requests
        assert (write.method, write.url.path, json.loads(write.content)) == (
            "PUT",
            "/api/selection/bulb-a",
            {"selected": selected},
        )


def test_each_action_needs_its_own_decision_and_foreign_or_wrong_prompt_cannot_resume(
    make_bot: BotFactory,
    tmp_path: Path,
) -> None:

    telegram = FakeTelegram()
    keeper = FakeKeeper({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor", "args": {"entity_id": "bulb-a"}, "id": "first"},
                    {"name": "stop_monitoring", "args": {"entity_id": "bulb-b"}, "id": "second"},
                ],
            ),
            AIMessage("Mixed decisions complete."),
        ],
        keeper,
    )
    bot = make_bot(
        family_settings(tmp_path, allowed_user_ids=frozenset({11, 22})),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Change both"))
    first, second = telegram.bodies()
    approve = first["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    reject = second["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    bot.handle_update(clicked(2, 22, approve, 101))
    bot.handle_update(clicked(3, 11, approve, 999))
    bot.handle_update(clicked(4, 11, "malformed", 101))
    assert len(agent.submissions()) == 1
    assert keeper.requests == []
    bot.handle_update(clicked(5, 11, approve, 101))
    assert len(agent.submissions()) == 1
    assert keeper.requests == []
    bot.handle_update(clicked(6, 11, reject, 102))
    bot.close()
    assert len(agent.submissions()) == 2
    [write] = keeper.requests
    assert (write.url.path, json.loads(write.content)) == (
        "/api/selection/bulb-a",
        {"selected": True},
    )
    assert telegram.bodies()[-1]["text"] == "Mixed decisions complete."


def test_plain_text_reply_contains_only_current_final_answer_and_splits_without_loss(
    make_bot: BotFactory,
    tmp_path: Path,
) -> None:

    telegram = FakeTelegram()
    text = "<script>Kitchen & <b>lamp</b></script> 😀" * 200
    agent = GraphAegra(
        [
            AIMessage("Old answer"),
            AIMessage(
                content=[
                    {"type": "reasoning", "reasoning": "PRIVATE INTERNAL"},
                    {"type": "text", "text": text},
                ]
            ),
        ],
        FakeKeeper(),
    )
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "First"))
    bot.handle_update(incoming(2, 11, "Now the final answer"))
    bot.close()
    replies = telegram.bodies()[1:]
    assert len(replies) >= 2
    assert all(0 < len(body["text"]) <= 4096 and "parse_mode" not in body for body in replies)
    assert "".join(body["text"] for body in replies) == text


def test_uncertain_submitted_approval_is_not_replayed_after_restart(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            monitoring_request(),
            AIMessage("Done."),
        ],
        keeper,
    )
    settings = family_settings(tmp_path)
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "Monitor"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    agent.fail_after_next_run = True
    bot.handle_update(clicked(2, 11, data, 101))
    assert len(keeper.requests) == 1
    bot.close()
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(clicked(2, 11, data, 101))
    bot.handle_update(clicked(3, 11, data, 101))
    bot.close()
    assert len(keeper.requests) == 1
    assert len(agent.submissions()) == 2
    assert "uncertain" in telegram.bodies()[1]["text"].lower()


def test_new_and_removed_access_make_old_pending_buttons_unusable(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper()
    agent = GraphAegra(
        [monitoring_request()],
        keeper,
    )
    settings = family_settings(tmp_path, allowed_user_ids=frozenset({11, 22}))
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "Monitor"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.close()
    removed = settings.model_copy(update={"allowed_user_ids": frozenset({22})})
    bot = make_bot(removed, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(clicked(2, 11, data, 101))
    bot.close()
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(3, 11, "/new"))
    bot.handle_update(clicked(4, 11, data, 101))
    bot.close()
    assert len(agent.submissions()) == 1
    assert keeper.requests == []


@pytest.mark.parametrize(
    "answer", [AIMessage(""), AIMessage(content=[{"type": "reasoning", "reasoning": "internal"}])]
)
def test_empty_or_internal_only_agent_answer_gets_a_fixed_failure(
    make_bot: BotFactory, tmp_path: Path, answer: AIMessage
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Old answer"), answer], FakeKeeper())
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "First"))
    bot.handle_update(incoming(2, 11, "Second"))
    bot.close()
    assert (
        telegram.bodies()[-1]["text"]
        == "The agent could not answer. Please try a new question or use /new."
    )


def test_lost_submission_is_not_replayed_but_later_batch_updates_are_not_skipped(
    make_bot: BotFactory,
    tmp_path: Path,
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Family answer")], FakeKeeper())
    settings = family_settings(tmp_path, allowed_user_ids=frozenset({11, 22}))
    updates = [incoming(1, 11, "First"), incoming(2, 22, "Family")]

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": updates})
        return telegram.handle(request)

    bot = make_bot(
        settings,
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    agent.fail_next_run = True
    bot.poll_once()
    bot.close()
    bot = make_bot(
        settings,
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    bot.poll_once()
    bot.close()
    assert len(agent.submissions()) == 2
    assert telegram.bodies()[-1]["text"] == "Family answer"


def test_process_exit_during_first_submission_keeps_second_fetched_update_available(
    make_bot: BotFactory,
    tmp_path: Path,
) -> None:
    class ProcessExit(BaseException):
        pass

    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Family answer")], FakeKeeper())
    updates = [incoming(1, 11, "First"), incoming(2, 22, "Family")]
    settings = family_settings(tmp_path, allowed_user_ids=frozenset({11, 22}))

    def telegram_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": updates})
        return telegram.handle(request)

    def agent_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs/wait"):
            raise ProcessExit
        return agent.handle(request)

    bot = make_bot(
        settings,
        telegram_client=httpx.Client(transport=httpx.MockTransport(telegram_transport)),
        agent_client=httpx.Client(transport=httpx.MockTransport(agent_transport)),
    )
    with pytest.raises(ProcessExit):
        bot.poll_once()
    bot.close()
    bot = make_bot(
        settings,
        telegram_client=httpx.Client(transport=httpx.MockTransport(telegram_transport)),
        agent_client=agent.client(),
    )
    bot.poll_once()
    bot.close()
    assert len(agent.submissions()) == 1
    assert telegram.bodies()[-1]["text"] == "Family answer"


def test_unknown_interrupt_fails_closed_without_buttons_or_selection_write(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper()
    agent = GraphAegra(
        [monitoring_request()],
        keeper,
    )

    def transport(request: httpx.Request) -> httpx.Response:
        response = agent.handle(request)
        if request.url.path.endswith("/state"):
            body: dict[str, object] = response.json()
            body["tasks"] = (
                [
                    {
                        "interrupts": [
                            {"id": "unknown", "value": {"unexpected": "future graph policy"}}
                        ]
                    }
                ]
                if agent.submissions()
                else []
            )
            return httpx.Response(200, json=body)
        return response

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    bot.handle_update(incoming(1, 11, "Monitor"))
    bot.handle_update(incoming(2, 11, "Continue"))
    bot.close()
    assert len(agent.submissions()) == 1
    assert keeper.requests == []
    assert "Unsupported approval" in telegram.bodies()[0]["text"]
    assert all("reply_markup" not in body for body in telegram.bodies())


@pytest.mark.parametrize("mismatch", ["chat", "interrupt", "action"])
def test_callback_must_match_the_actual_chat_interrupt_and_action(
    make_bot: BotFactory, tmp_path: Path, mismatch: str
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper()
    agent = GraphAegra(
        [
            monitoring_request(),
            AIMessage("Rejected."),
        ],
        keeper,
    )
    changed = False

    def transport(request: httpx.Request) -> httpx.Response:
        response = agent.handle(request)
        if changed and request.url.path.endswith("/state") and mismatch != "chat":
            body = response.json()
            interrupt = body["tasks"][0]["interrupts"][0]
            if mismatch == "interrupt":
                interrupt["id"] = "different-interrupt"
            else:
                interrupt["value"]["action_requests"][0]["args"]["entity_id"] = "bulb-other"
            return httpx.Response(200, json=body)
        return response

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    bot.handle_update(incoming(1, 11, "Monitor"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    changed = True
    callback = clicked(2, 11, data, 101)
    if mismatch == "chat":
        callback = clicked(2, 11, data, 101, chat_id=22)
    bot.handle_update(callback)
    assert len(agent.submissions()) == 1
    assert keeper.requests == []
    assert telegram.bodies()[-1]["text"] == STALE
    changed = False
    reject = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    bot.handle_update(clicked(3, 11, reject, 101))
    bot.close()
    assert len(agent.submissions()) == 2
    assert keeper.requests == []
    assert telegram.bodies()[-1]["text"] == "Rejected."


def test_known_rate_limit_waits_before_retry_without_submitting_an_agent_run(
    make_bot: BotFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delays: list[float] = []
    monkeypatch.setattr("time.sleep", delays.append)
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 7}}
            )
        return httpx.Response(200, json={"ok": True, "result": []})

    agent = GraphAegra([AIMessage("Unused")], FakeKeeper())
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    bot.poll_once()
    bot.close()
    assert delays == [7]
    assert len(requests) == 2
    assert agent.requests == []


@pytest.mark.parametrize("error_code", [401, 404, 409])
def test_invalid_token_or_polling_conflict_has_fixed_secret_free_error(
    make_bot: BotFactory, tmp_path: Path, error_code: int
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            error_code,
            json={"ok": False, "error_code": error_code, "description": "private-secret"},
        )

    bot = make_bot(
        family_settings(tmp_path, bot_token=SecretStr("123:private-secret")),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(FatalTelegramError) as caught:
        bot.poll_once()
    bot.close()
    assert "private-secret" not in str(caught.value)


def test_preexisting_webhook_is_reported_without_deleting_it(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"url": "https://other-deployment.test/webhook"}}
        )

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(TransportError, match="webhook is configured"):
        bot.validate_startup()
    bot.close()
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/getWebhookInfo")


def test_help_attachments_and_edited_messages_do_not_call_the_agent(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Unused")], FakeKeeper())
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "/start"))
    bot.handle_update(incoming(2, 11, "/help"))
    bot.handle_update(
        {
            "update_id": 3,
            "message": {
                "message_id": 3,
                "from": {"id": 11},
                "chat": {"id": 11, "type": "private"},
                "photo": [{"file_id": "fake-photo"}],
            },
        }
    )
    bot.handle_update(
        {
            "update_id": 4,
            "edited_message": {
                "message_id": 3,
                "from": {"id": 11},
                "chat": {"id": 11, "type": "private"},
                "text": "Monitor this edited message",
            },
        }
    )
    bot.close()
    assert agent.requests == []
    bodies = telegram.bodies()
    assert len(bodies) == 3
    assert "/new" in bodies[0]["text"]
    assert bodies[0]["text"] == bodies[1]["text"]
    assert bodies[2]["text"] == "Please send a text question; attachments are not supported."


def test_lost_unsubmitted_approval_does_not_become_a_retry_after_restart(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper()
    agent = GraphAegra(
        [monitoring_request()],
        keeper,
    )
    settings = family_settings(tmp_path)
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "Monitor"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    agent.fail_next_run = True
    bot.handle_update(clicked(2, 11, data, 101))
    bot.close()
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(clicked(3, 11, data, 101))
    bot.handle_update(incoming(4, 11, "Please continue"))
    bot.close()
    assert len(agent.submissions()) == 2
    assert keeper.requests == []
    assert "uncertain" in telegram.bodies()[-1]["text"].lower()


def test_expired_callback_acknowledgement_does_not_discard_a_valid_approval(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            monitoring_request(),
            AIMessage("Monitoring approved."),
        ],
        keeper,
    )

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/answerCallbackQuery"):
            return httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "query too old"},
            )
        return telegram.handle(request)

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Monitor this light"))
    assert keeper.requests == []
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update(clicked(2, 11, data, 101))
    bot.handle_update(clicked(3, 11, data, 101))
    bot.close()

    assert len(agent.submissions()) == 2
    [write] = keeper.requests
    assert (write.method, write.url.path, json.loads(write.content)) == (
        "PUT",
        "/api/selection/bulb-a",
        {"selected": True},
    )
    replies = [body["text"] for body in telegram.bodies()]
    assert "Monitoring approved." in replies
    assert all("uncertain" not in reply.lower() for reply in replies)


def test_malformed_unicode_action_index_does_not_crash_or_consume_the_valid_button(
    make_bot: BotFactory,
    tmp_path: Path,
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            monitoring_request(),
            AIMessage("Monitoring approved."),
        ],
        keeper,
    )
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Monitor this light"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    token, _, decision = data.split(":")
    bot.handle_update(clicked(2, 11, f"{token}:²:{decision}", 101))
    assert len(agent.submissions()) == 1
    assert keeper.requests == []
    bot.handle_update(clicked(3, 11, data, 101))
    bot.close()
    assert len(agent.submissions()) == 2
    assert len(keeper.requests) == 1
    assert telegram.bodies()[-1]["text"] == "Monitoring approved."


@pytest.mark.parametrize("status", ["pending", "running"])
def test_uncertain_active_run_blocks_same_user_until_finished(
    make_bot: BotFactory, tmp_path: Path, status: str
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Finished answer"), AIMessage("Follow-up answer")], FakeKeeper())
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    agent.fail_after_next_run = True
    bot.handle_update(incoming(1, 11, "First question"))
    agent.runs = [{"status": status}]
    bot.handle_update(incoming(2, 11, "Are you done?"))
    assert len(agent.submissions()) == 1
    assert "uncertain" in telegram.bodies()[-1]["text"].lower()
    agent.runs = [{"status": "success"}]
    bot.handle_update(incoming(3, 11, "Now continue"))
    bot.close()
    assert len(agent.submissions()) == 2
    assert telegram.bodies()[-1]["text"] == "Follow-up answer"


def test_token_rotation_keeps_same_bot_state_but_other_bot_cannot_own_it(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("First answer"), AIMessage("Follow-up answer")], FakeKeeper())
    settings = family_settings(tmp_path, bot_token=SecretStr("123:old-secret"))
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.validate_startup()
    bot.handle_update(incoming(1, 11, "First"))
    bot.close()
    rotated = settings.model_copy(update={"bot_token": SecretStr("123:new-secret")})
    bot = make_bot(rotated, telegram_client=telegram.client(), agent_client=agent.client())
    bot.validate_startup()
    bot.handle_update(incoming(1, 11, "First"))
    bot.handle_update(incoming(2, 11, "Follow-up"))
    bot.close()
    submissions = agent.submissions()
    assert len(submissions) == 2
    assert submissions[0].url.path == submissions[1].url.path
    telegram.bot_id = 99999
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    with pytest.raises(TransportError, match="different bot"):
        bot.validate_startup()
    bot.close()
    telegram.bot_id = 12345
    bot = make_bot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.validate_startup()
    bot.close()


@pytest.mark.parametrize("method", ["sendMessage", "answerCallbackQuery"])
@pytest.mark.parametrize("code", [401, 404, 409])
def test_fatal_telegram_errors_escape_update_handling(
    make_bot: BotFactory, tmp_path: Path, method: str, code: int
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/" + method)
        return httpx.Response(code, json={"ok": False, "error_code": code})

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    update = incoming(1, 11, "/help") if method == "sendMessage" else clicked(1, 11, "stale", 101)
    try:
        with pytest.raises(FatalTelegramError):
            bot.handle_update(update)
    finally:
        bot.close()


@pytest.mark.parametrize("retry_after", ["missing", None, -1, 0, "soon", 47])
def test_consecutive_rate_limits_retry_once_with_fallback_then_return_to_poller(
    make_bot: BotFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_after: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr("time.sleep", delays.append)
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "parameters": {} if retry_after == "missing" else {"retry_after": retry_after},
            },
        )

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    try:
        with pytest.raises(PollError) as caught:
            bot.poll_once()
        assert caught.value.retry_after == 1
    finally:
        bot.close()
    assert len(requests) == 2
    assert delays == ([30, 17, 30, 17] if retry_after == 47 else [1, 1])
    assert f"Telegram rate limit: waiting {47 if retry_after == 47 else 1} seconds" in caplog.text


@pytest.mark.parametrize(
    "shape", ["count", "name", "missing_reject", "entity", "multiple_interrupts"]
)
def test_malformed_initial_approval_has_no_buttons_or_selection_write(
    make_bot: BotFactory, tmp_path: Path, shape: str
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper()
    agent = GraphAegra(
        [monitoring_request()],
        keeper,
    )

    def transport(request: httpx.Request) -> httpx.Response:
        response = agent.handle(request)
        if request.url.path.endswith("/state") and agent.submissions():
            body = response.json()
            interrupts = body["tasks"][0]["interrupts"]
            value = interrupts[0]["value"]
            if shape == "count":
                value["review_configs"] = []
            elif shape == "name":
                value["review_configs"][0]["action_name"] = "stop_monitoring"
            elif shape == "missing_reject":
                value["review_configs"][0]["allowed_decisions"] = ["approve"]
            elif shape == "entity":
                value["action_requests"][0]["args"]["entity_id"] = 123
            else:
                interrupts.append({"id": "second", "value": value})
            return httpx.Response(200, json=body)
        return response

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    bot.handle_update(incoming(1, 11, "Monitor"))
    bot.close()
    assert len(agent.submissions()) == 1
    assert keeper.requests == []
    [reply] = telegram.bodies()
    assert "Unsupported approval" in reply["text"]
    assert "reply_markup" not in reply


def test_split_approval_puts_buttons_on_final_chunk_and_removes_them_after_decision(
    make_bot: BotFactory,
    tmp_path: Path,
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra(
        [
            monitoring_request(),
            AIMessage("Rejected."),
        ],
        FakeKeeper(),
    )

    def transport(request: httpx.Request) -> httpx.Response:
        response = agent.handle(request)
        if request.url.path.endswith("/state") and agent.submissions():
            body = response.json()
            if body["tasks"][0]["interrupts"]:
                body["tasks"][0]["interrupts"][0]["value"]["action_requests"][0]["description"] = (
                    "Lamp name " * 1000
                )
            return httpx.Response(200, json=body)
        return response

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    bot.handle_update(incoming(1, 11, "Monitor"))
    prompts = telegram.bodies()
    assert len(prompts) > 1
    assert all("reply_markup" not in prompt for prompt in prompts[:-1])
    assert all(0 < len(prompt["text"]) <= 4096 for prompt in prompts)
    data = prompts[-1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"]
    message_id = 100 + len(prompts)
    bot.handle_update(clicked(2, 11, data, message_id))
    bot.close()
    [edit] = telegram.bodies("editMessageReplyMarkup")
    assert edit == {
        "chat_id": 11,
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": []},
    }
    assert telegram.bodies()[-1]["text"] == "Rejected."


@pytest.mark.parametrize("failure", ["network", "malformed_json", "server_error", "non_ok"])
def test_transient_telegram_boundary_errors_are_nonfatal_and_secret_free(
    make_bot: BotFactory, tmp_path: Path, failure: str
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError("123:secret Private payload", request=request)
        if failure == "malformed_json":
            return httpx.Response(200, content=b"123:secret not-json")
        if failure == "server_error":
            return httpx.Response(
                503, json={"ok": False, "error_code": 503, "description": "123:secret"}
            )
        return httpx.Response(200, json={"ok": False, "description": "123:secret"})

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(PollError) as caught:
        bot.poll_once()
    assert "123:secret" not in str(caught.value)
    assert "Private" not in str(caught.value)


def test_polling_advances_outgoing_offset_after_handled_update(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    requests: list[TelegramBody] = []
    telegram = FakeTelegram()

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            requests.append(telegram_body(request))
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [incoming(5, 11, "/help")] if len(requests) == 1 else [],
                },
            )
        return telegram.handle(request)

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    bot.poll_once()
    bot.poll_once()
    assert [request["offset"] for request in requests] == [0, 6]


def test_failed_final_delivery_allows_new_question_without_replaying_previous_run(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("First answer"), AIMessage("Second answer")], FakeKeeper())
    sends = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        if request.url.path.endswith("/sendMessage"):
            sends += 1
            if sends == 1:
                raise httpx.ReadTimeout("Ambiguous delivery", request=request)
        return telegram.handle(request)

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "First question"))
    bot.handle_update(incoming(2, 11, "Second question"))
    assert len(agent.submissions()) == 2
    assert sends == 3  # Ambiguous first send, uncertainty notice, new answer; no answer resend.
    assert telegram.bodies()[-1]["text"] == "Second answer"


@pytest.mark.parametrize("operation", ["fallback", "keyboard_edit"])
def test_fatal_telegram_delivery_during_recovery_or_cleanup_stops_worker(
    make_bot: BotFactory, tmp_path: Path, operation: str
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper()
    agent = GraphAegra(
        [monitoring_request()],
        keeper,
    )

    def transport(request: httpx.Request) -> httpx.Response:
        target = "sendMessage" if operation == "fallback" else "editMessageReplyMarkup"
        if request.url.path.endswith("/" + target):
            return httpx.Response(401, json={"ok": False, "error_code": 401})
        return telegram.handle(request)

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    if operation == "fallback":
        agent.fail_next_run = True
        update = incoming(1, 11, "Question")
    else:
        bot.handle_update(incoming(1, 11, "Monitor"))
        data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        update = clicked(2, 11, data, 101)
    with pytest.raises(FatalTelegramError):
        bot.handle_update(update)
    assert keeper.requests == []


def test_boolean_sent_message_id_is_not_a_valid_approval_reference(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra(
        [monitoring_request()],
        FakeKeeper(),
    )
    # Captured Telegram HTTP payloads are parsed at this test boundary.
    sent: list[TelegramBody] = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            sent.append(telegram_body(request))
            if len(sent) == 1:
                return httpx.Response(200, json={"ok": True, "result": {"message_id": True}})
        return telegram.handle(request)

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Monitor"))
    saved = json.loads(
        bot.db.execute("SELECT state FROM conversations WHERE user_id=11").fetchone()[0]
    )
    assert saved["pending"]["message_ids"] == []
    assert sent[1]["text"] == UNCERTAIN
    data = sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update(clicked(2, 11, data, 1))
    assert len(agent.submissions()) == 1


def test_failure_to_remove_resolved_keyboard_still_resumes_approved_graph(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = FakeKeeper({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra([monitoring_request(), AIMessage("Approved.")], keeper)

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/editMessageReplyMarkup"):
            return httpx.Response(400, json={"ok": False, "error_code": 400})
        return telegram.handle(request)

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Monitor"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update(clicked(2, 11, data, 101))
    bot.handle_update(clicked(3, 11, data, 101))
    assert len(agent.submissions()) == 2
    assert len(keeper.requests) == 1
    assert telegram.bodies()[-1]["text"] == STALE
    assert "Approved." in [body["text"] for body in telegram.bodies()]


def light_keeper(state: str = "on") -> FakeKeeper:
    return FakeKeeper(
        {
            "/api/monitored": lights_response(state),
            "/api/control": control_response("on"),
            "/api/devices/bulb-a/command": command_response("observed"),
        }
    )


def light_call(name: str, args: dict[str, object]) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": "light"}])


NOT_ALLOWED = "You are not allowed to control lights. Nothing was changed."


@pytest.mark.parametrize(
    ("name", "args", "state", "first_line"),
    [
        ("turn_off", {"entity_id": "bulb-a"}, "on", "Turn off bulb-a"),
        ("turn_on", {"entity_id": "bulb-a"}, "off", "Turn on bulb-a"),
        (
            "turn_on",
            {"entity_id": "bulb-a", "brightness_percent": 30},
            "off",
            "Turn on bulb-a at 30%",
        ),
    ],
)
def test_light_command_cards_say_what_will_happen(
    make_bot: BotFactory,
    tmp_path: Path,
    *,
    name: str,
    args: dict[str, object],
    state: str,
    first_line: str,
) -> None:
    telegram = FakeTelegram()
    keeper = light_keeper(state)
    agent = GraphAegra([light_call(name, args), AIMessage("Done.")], keeper)
    bot = make_bot(
        family_settings(tmp_path, control_user_ids=frozenset({11})),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )

    bot.handle_update(incoming(1, 11, "Change the light"))

    [card] = telegram.bodies()
    assert card["text"].splitlines()[0] == first_line
    assert [b["text"] for b in card["reply_markup"]["inline_keyboard"][0]] == ["Approve", "Reject"]
    assert command_requests(keeper) == []


def test_every_run_and_resume_carries_the_sender_and_whether_they_may_control(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = light_keeper()
    agent = GraphAegra(
        [light_call("turn_off", {"entity_id": "bulb-a"}), AIMessage("Done.")], keeper
    )
    bot = make_bot(
        family_settings(
            tmp_path, allowed_user_ids=frozenset({11, 22}), control_user_ids=frozenset({11})
        ),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )

    bot.handle_update(incoming(1, 11, "Turn it off", first_name="Ali", last_name="Rezaei"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update(clicked(2, 11, data, 101, first_name="Ali", last_name="Rezaei"))
    bot.handle_update(incoming(3, 22, "How long was it on?", first_name="Sam"))
    bot.handle_update(incoming(4, 22, "Again"))

    ali = {"kind": "telegram", "id": 11, "name": "Ali Rezaei"}
    assert [(c["sender"], c["may_control"]) for c in agent.configurables] == [
        (ali, True),
        (ali, True),
        ({"kind": "telegram", "id": 22, "name": "Sam"}, False),
        # Telegram sends no name for this user: the numeric ID stands in.
        ({"kind": "telegram", "id": 22, "name": "22"}, False),
    ]


def test_control_user_approval_sends_one_command_attributed_to_them(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = light_keeper()
    agent = GraphAegra(
        [light_call("turn_off", {"entity_id": "bulb-a"}), AIMessage("The light is off.")],
        keeper,
    )
    bot = make_bot(
        family_settings(tmp_path, control_user_ids=frozenset({11})),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )

    bot.handle_update(incoming(1, 11, "Turn it off", first_name="Ali"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update(clicked(2, 11, data, 101, first_name="Ali"))

    [command] = command_requests(keeper)
    assert command.url.path == "/api/devices/bulb-a/command"
    assert json.loads(command.content) == {
        "command": {"action": "off"},
        "sender": {"kind": "telegram", "id": 11, "name": "Ali"},
    }
    assert telegram.bodies()[-1]["text"] == "The light is off."


def test_allowed_user_without_control_gets_no_card_and_no_command(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = light_keeper()
    agent = GraphAegra(
        [light_call("turn_off", {"entity_id": "bulb-a"}), AIMessage("Not allowed.")], keeper
    )
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )

    bot.handle_update(incoming(1, 11, "Turn it off"))

    [reply] = telegram.bodies()
    assert reply["text"] == "Not allowed."
    assert "reply_markup" not in reply
    assert command_requests(keeper) == []


@pytest.mark.parametrize(
    ("name", "args", "state"),
    [
        ("turn_off", {"entity_id": "bulb-a"}, "on"),
        ("turn_on", {"entity_id": "bulb-a"}, "off"),
        ("turn_on", {"entity_id": "bulb-a", "brightness_percent": 30}, "off"),
    ],
)
def test_approve_by_a_user_who_lost_control_is_rejected_and_the_chat_continues(
    make_bot: BotFactory, tmp_path: Path, name: str, args: dict[str, object], state: str
) -> None:
    telegram = FakeTelegram()
    keeper = light_keeper(state)
    agent = GraphAegra(
        [
            light_call(name, args),
            AIMessage("Nothing changed."),
            AIMessage("It was on for two hours."),
        ],
        keeper,
    )
    bot = make_bot(
        family_settings(tmp_path, control_user_ids=frozenset({11})),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Turn it off"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.close()
    # The operator removes the user from the control list while the card is open.
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )

    bot.handle_update(clicked(2, 11, data, 101))

    assert command_requests(keeper) == []
    assert [c["may_control"] for c in agent.configurables] == [True, False]
    resume = json.loads(agent.submissions()[-1].content)
    assert resume["command"]["resume"] == {"decisions": [{"type": "reject"}]}
    texts = [body["text"] for body in telegram.bodies()]
    assert NOT_ALLOWED in texts
    assert "Nothing changed." in texts

    bot.handle_update(incoming(3, 11, "How long was it on?"))

    assert telegram.bodies()[-1]["text"] == "It was on for two hours."
    assert REMINDER not in [body["text"] for body in telegram.bodies()]
    assert command_requests(keeper) == []


def test_failed_refusal_notice_still_resumes_the_rejected_approval(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = light_keeper()
    agent = GraphAegra(
        [
            light_call("turn_off", {"entity_id": "bulb-a"}),
            AIMessage("Nothing changed."),
            AIMessage("It was on for two hours."),
        ],
        keeper,
    )

    def transport(request: httpx.Request) -> httpx.Response:
        if (
            request.url.path.endswith("/sendMessage")
            and json.loads(request.content).get("text") == NOT_ALLOWED
        ):
            telegram.requests.append(request)
            return httpx.Response(500, json={"ok": False, "error_code": 500})
        return telegram.handle(request)

    bot = make_bot(
        family_settings(tmp_path, control_user_ids=frozenset({11})),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Turn it off"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.close()
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )

    bot.handle_update(clicked(2, 11, data, 101))

    assert len(agent.submissions()) == 2, "rejected approval was never resumed"
    bot.handle_update(incoming(3, 11, "How long was it on?"))
    assert telegram.bodies()[-1]["text"] == "It was on for two hours."
    assert REMINDER not in [body["text"] for body in telegram.bodies()]


@pytest.mark.parametrize("error_code", [401, 404])
def test_fatal_error_on_the_refusal_notice_still_resumes_the_rejected_approval(
    make_bot: BotFactory, tmp_path: Path, error_code: int
) -> None:
    telegram = FakeTelegram()
    keeper = light_keeper()
    agent = GraphAegra(
        [
            light_call("turn_off", {"entity_id": "bulb-a"}),
            AIMessage("Nothing changed."),
            AIMessage("It was on for two hours."),
        ],
        keeper,
    )

    def transport(request: httpx.Request) -> httpx.Response:
        if (
            request.url.path.endswith("/sendMessage")
            and json.loads(request.content).get("text") == NOT_ALLOWED
        ):
            return httpx.Response(error_code, json={"ok": False, "error_code": error_code})
        return telegram.handle(request)

    bot = make_bot(
        family_settings(tmp_path, control_user_ids=frozenset({11})),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Turn it off"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.close()
    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )

    # The worker may stop on the fatal error, but not before the reject reaches the agent.
    with suppress(FatalTelegramError):
        bot.handle_update(clicked(2, 11, data, 101))
    bot.close()
    assert len(agent.submissions()) == 2, "rejected approval was never resumed"

    bot = make_bot(
        family_settings(tmp_path),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(3, 11, "How long was it on?"))

    assert telegram.bodies()[-1]["text"] == "It was on for two hours."
    assert REMINDER not in [body["text"] for body in telegram.bodies()]
    assert command_requests(keeper) == []


def plug_keeper(state: str = "off") -> FakeKeeper:
    return FakeKeeper(
        {
            "/api/monitored": switch_response(state),
            "/api/control": control_response("on"),
            "/api/devices/plug-a/command": command_response("observed", "on"),
        }
    )


def tool_results(agent: GraphAegra) -> list[str]:
    """The tool results in the thread of the agent's last run."""
    thread_id = agent.submissions()[-1].url.path.split("/")[2]
    state = agent.graph.get_state({"configurable": {"thread_id": thread_id}})
    return [m.text for m in state.values["messages"] if isinstance(m, ToolMessage)]


def test_control_user_approves_a_plug_command_and_it_is_attributed_to_them(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = plug_keeper("off")
    agent = GraphAegra(
        [light_call("turn_on", {"entity_id": "plug-a"}), AIMessage("The kettle is on.")], keeper
    )
    bot = make_bot(
        family_settings(tmp_path, control_user_ids=frozenset({11})),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )

    bot.handle_update(incoming(1, 11, "Turn the kettle on", first_name="Ali"))

    [card] = telegram.bodies()
    assert "plug-a" in card["text"].splitlines()[0]
    buttons = card["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Approve", "Reject"]
    assert command_requests(keeper) == []

    bot.handle_update(clicked(2, 11, buttons[0]["callback_data"], 101, first_name="Ali"))

    [command] = command_requests(keeper)
    assert command.url.path == "/api/devices/plug-a/command"
    assert json.loads(command.content) == {
        "command": {"action": "on"},
        "sender": {"kind": "telegram", "id": 11, "name": "Ali"},
    }
    assert telegram.bodies()[-1]["text"] == "The kettle is on."


def test_user_not_on_the_control_list_is_refused_a_plug_command_without_buttons(
    make_bot: BotFactory, tmp_path: Path
) -> None:
    telegram = FakeTelegram()
    keeper = plug_keeper("off")
    agent = GraphAegra(
        [light_call("turn_on", {"entity_id": "plug-a"}), AIMessage("Not allowed.")], keeper
    )
    bot = make_bot(
        family_settings(
            tmp_path, allowed_user_ids=frozenset({11, 22}), control_user_ids=frozenset({11})
        ),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )

    bot.handle_update(incoming(1, 22, "Turn the kettle on"))

    assert tool_results(agent) == [TOOL_NOT_ALLOWED]
    [reply] = telegram.bodies()
    assert reply["text"] == "Not allowed."
    assert "reply_markup" not in reply
    assert command_requests(keeper) == []
