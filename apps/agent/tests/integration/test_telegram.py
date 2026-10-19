import json
from pathlib import Path

import httpx
import pytest
from agent_support import FakeObserver, snapshot
from langchain_core.messages import AIMessage
from pydantic import SecretStr
from telegram_support import FakeTelegram, GraphAegra, clicked, incoming

from home_observer_agent.telegram import PollError, Settings, TelegramBot, TransportError


def message(update_id: int, user_id: int, text: str, *, kind: str = "private") -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": kind},
            "text": text,
        },
    }


def test_only_allowed_private_messages_can_reach_the_agent(tmp_path: Path) -> None:
    agent_requests: list[httpx.Request] = []
    telegram_requests: list[dict[str, object]] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        telegram_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def agent(request: httpx.Request) -> httpx.Response:
        agent_requests.append(request)
        raise AssertionError("Unauthorized messages must not reach Aegra")

    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11, 22}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=httpx.Client(transport=httpx.MockTransport(telegram)),
        agent_client=httpx.Client(transport=httpx.MockTransport(agent)),
    )
    bot.handle_update(message(1, 99, "What are my lights?"))
    bot.handle_update(message(2, 11, "What are my lights?", kind="group"))
    bot.handle_update(message(3, 99, "/start"))
    bot.close()

    assert agent_requests == []
    assert len(telegram_requests) == 1
    assert telegram_requests[0]["chat_id"] == 99
    assert "99" in str(telegram_requests[0]["text"])


def test_family_members_keep_separate_saved_threads_and_new_preserves_old_history(
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
        FakeObserver(),
    )
    settings = Settings(
        bot_token=SecretStr("123:secret"),
        allowed_user_ids=frozenset({11, 22}),
        state_path=str(tmp_path / "bot.sqlite3"),
    )
    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "First question"))
    bot.handle_update(incoming(2, 22, "Family question"))
    bot.close()
    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
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
def test_telegram_buttons_resume_real_monitoring_action_after_restart(
    tmp_path: Path, decision: str
) -> None:

    telegram = FakeTelegram()
    observer = FakeObserver({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor_light", "args": {"entity_id": "bulb-a"}, "id": "call-select"}
                ],
            ),
            AIMessage("Done."),
        ],
        observer,
    )
    settings = Settings(
        bot_token=SecretStr("123:secret"),
        allowed_user_ids=frozenset({11}),
        state_path=str(tmp_path / "bot.sqlite3"),
    )
    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "Watch this light"))
    assert observer.requests == []
    [prompt] = telegram.bodies()
    assert "bulb-a" in prompt["text"]
    buttons = prompt["reply_markup"]["inline_keyboard"][0]
    data = next(button["callback_data"] for button in buttons if button["text"].lower() == decision)
    bot.close()

    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(2, 11, "Please continue"))
    assert len(agent.submissions()) == 1
    bot.handle_update(clicked(3, 11, data, 101))
    bot.handle_update(clicked(4, 11, data, 101))
    bot.close()

    assert len(agent.submissions()) == 2
    assert telegram.bodies()[-1]["text"] != "Done."  # repeated button is stale, not another answer
    assert "Done." in [body["text"] for body in telegram.bodies()]
    assert len(telegram.bodies("answerCallbackQuery")) == 2
    if decision == "reject":
        assert observer.requests == []
    else:
        [write] = observer.requests
        assert (write.method, write.url.path, json.loads(write.content)) == (
            "PUT",
            "/api/selection/bulb-a",
            {"selected": True},
        )


def test_each_action_needs_its_own_decision_and_foreign_or_wrong_prompt_cannot_resume(
    tmp_path: Path,
) -> None:

    telegram = FakeTelegram()
    observer = FakeObserver({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor_light", "args": {"entity_id": "bulb-a"}, "id": "first"},
                    {"name": "stop_monitoring", "args": {"entity_id": "bulb-b"}, "id": "second"},
                ],
            ),
            AIMessage("Mixed decisions complete."),
        ],
        observer,
    )
    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11, 22}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
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
    assert observer.requests == []
    bot.handle_update(clicked(5, 11, approve, 101))
    assert len(agent.submissions()) == 1
    assert observer.requests == []
    bot.handle_update(clicked(6, 11, reject, 102))
    bot.close()
    assert len(agent.submissions()) == 2
    [write] = observer.requests
    assert (write.url.path, json.loads(write.content)) == (
        "/api/selection/bulb-a",
        {"selected": True},
    )
    assert telegram.bodies()[-1]["text"] == "Mixed decisions complete."


def test_plain_text_reply_contains_only_current_final_answer_and_splits_without_loss(
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
        FakeObserver(),
    )
    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
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


def test_uncertain_submitted_approval_is_not_replayed_after_restart(tmp_path: Path) -> None:
    telegram = FakeTelegram()
    observer = FakeObserver({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor_light", "args": {"entity_id": "bulb-a"}, "id": "select"}
                ],
            ),
            AIMessage("Done."),
        ],
        observer,
    )
    settings = Settings(
        bot_token=SecretStr("123:secret"),
        allowed_user_ids=frozenset({11}),
        state_path=str(tmp_path / "bot.sqlite3"),
    )
    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "Monitor"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    agent.fail_after_next_run = True
    bot.handle_update(clicked(2, 11, data, 101))
    assert len(observer.requests) == 1
    bot.close()
    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(clicked(2, 11, data, 101))
    bot.handle_update(clicked(3, 11, data, 101))
    bot.close()
    assert len(observer.requests) == 1
    assert len(agent.submissions()) == 2
    assert "uncertain" in str(telegram.bodies()[1]["text"]).lower()


def test_new_and_removed_access_make_old_pending_buttons_unusable(tmp_path: Path) -> None:
    telegram = FakeTelegram()
    observer = FakeObserver()
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor_light", "args": {"entity_id": "bulb-a"}, "id": "select"}
                ],
            )
        ],
        observer,
    )
    settings = Settings(
        bot_token=SecretStr("123:secret"),
        allowed_user_ids=frozenset({11, 22}),
        state_path=str(tmp_path / "bot.sqlite3"),
    )
    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "Monitor"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.close()
    removed = settings.model_copy(update={"allowed_user_ids": frozenset({22})})
    bot = TelegramBot(removed, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(clicked(2, 11, data, 101))
    bot.close()
    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(3, 11, "/new"))
    bot.handle_update(clicked(4, 11, data, 101))
    bot.close()
    assert len(agent.submissions()) == 1
    assert observer.requests == []


@pytest.mark.parametrize(
    "answer", [AIMessage(""), AIMessage(content=[{"type": "reasoning", "reasoning": "internal"}])]
)
def test_empty_or_internal_only_agent_answer_gets_a_fixed_failure(
    tmp_path: Path, answer: AIMessage
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Old answer"), answer], FakeObserver())
    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
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
    tmp_path: Path,
) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Family answer")], FakeObserver())
    settings = Settings(
        bot_token=SecretStr("123:secret"),
        allowed_user_ids=frozenset({11, 22}),
        state_path=str(tmp_path / "bot.sqlite3"),
    )
    updates = [incoming(1, 11, "First"), incoming(2, 22, "Family")]

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": updates})
        return telegram.handle(request)

    bot = TelegramBot(
        settings,
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    agent.fail_next_run = True
    bot.poll_once()
    bot.close()
    bot = TelegramBot(
        settings,
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    bot.poll_once()
    bot.close()
    assert len(agent.submissions()) == 2
    assert telegram.bodies()[-1]["text"] == "Family answer"


def test_process_exit_during_first_submission_keeps_second_fetched_update_available(
    tmp_path: Path,
) -> None:
    class ProcessExit(BaseException):
        pass

    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Family answer")], FakeObserver())
    updates = [incoming(1, 11, "First"), incoming(2, 22, "Family")]
    settings = Settings(
        bot_token=SecretStr("123:secret"),
        allowed_user_ids=frozenset({11, 22}),
        state_path=str(tmp_path / "bot.sqlite3"),
    )

    def telegram_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": updates})
        return telegram.handle(request)

    def agent_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runs/wait"):
            raise ProcessExit
        return agent.handle(request)

    bot = TelegramBot(
        settings,
        telegram_client=httpx.Client(transport=httpx.MockTransport(telegram_transport)),
        agent_client=httpx.Client(transport=httpx.MockTransport(agent_transport)),
    )
    with pytest.raises(ProcessExit):
        bot.poll_once()
    bot.close()
    bot = TelegramBot(
        settings,
        telegram_client=httpx.Client(transport=httpx.MockTransport(telegram_transport)),
        agent_client=agent.client(),
    )
    bot.poll_once()
    bot.close()
    assert len(agent.submissions()) == 1
    assert telegram.bodies()[-1]["text"] == "Family answer"


def test_unknown_interrupt_fails_closed_without_buttons_or_selection_write(tmp_path: Path) -> None:
    telegram = FakeTelegram()
    observer = FakeObserver()
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor_light", "args": {"entity_id": "bulb-a"}, "id": "select"}
                ],
            )
        ],
        observer,
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

    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=telegram.client(),
        agent_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    bot.handle_update(incoming(1, 11, "Monitor"))
    bot.handle_update(incoming(2, 11, "Continue"))
    bot.close()
    assert len(agent.submissions()) == 1
    assert observer.requests == []
    assert "Unsupported approval" in str(telegram.bodies()[0]["text"])
    assert all("reply_markup" not in body for body in telegram.bodies())


@pytest.mark.parametrize("mismatch", ["chat", "interrupt", "action"])
def test_callback_must_match_the_actual_chat_interrupt_and_action(
    tmp_path: Path, mismatch: str
) -> None:
    telegram = FakeTelegram()
    observer = FakeObserver()
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor_light", "args": {"entity_id": "bulb-a"}, "id": "select"}
                ],
            )
        ],
        observer,
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

    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=telegram.client(),
        agent_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    bot.handle_update(incoming(1, 11, "Monitor"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    changed = True
    callback = clicked(2, 11, data, 101)
    if mismatch == "chat":
        callback = {
            "update_id": 2,
            "callback_query": {
                "id": "callback-2",
                "from": {"id": 11},
                "data": data,
                "message": {"message_id": 101, "chat": {"id": 22, "type": "private"}},
            },
        }
    bot.handle_update(callback)
    bot.close()
    assert len(agent.submissions()) == 1
    assert observer.requests == []


def test_known_rate_limit_waits_before_retry_without_submitting_an_agent_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    agent = GraphAegra([AIMessage("Unused")], FakeObserver())
    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    bot.poll_once()
    bot.close()
    assert delays == [7]
    assert len(requests) == 2
    assert agent.requests == []


@pytest.mark.parametrize("error_code", [401, 409])
def test_invalid_token_or_polling_conflict_has_fixed_secret_free_error(
    tmp_path: Path, error_code: int
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            error_code,
            json={"ok": False, "error_code": error_code, "description": "private-secret"},
        )

    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:private-secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(PollError) as caught:
        bot.poll_once()
    bot.close()
    assert caught.value.fatal
    assert "private-secret" not in str(caught.value)


def test_preexisting_webhook_is_reported_without_deleting_it(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"url": "https://other-deployment.test/webhook"}}
        )

    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(TransportError, match="webhook is configured"):
        bot.validate_startup()
    bot.close()
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/getWebhookInfo")


def test_help_attachments_and_edited_messages_do_not_call_the_agent(tmp_path: Path) -> None:
    telegram = FakeTelegram()
    agent = GraphAegra([AIMessage("Unused")], FakeObserver())
    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
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
    assert "text" in bodies[2]["text"]


def test_lost_unsubmitted_approval_does_not_become_a_retry_after_restart(tmp_path: Path) -> None:
    telegram = FakeTelegram()
    observer = FakeObserver()
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor_light", "args": {"entity_id": "bulb-a"}, "id": "select"}
                ],
            )
        ],
        observer,
    )
    settings = Settings(
        bot_token=SecretStr("123:secret"),
        allowed_user_ids=frozenset({11}),
        state_path=str(tmp_path / "bot.sqlite3"),
    )
    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(incoming(1, 11, "Monitor"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    agent.fail_next_run = True
    bot.handle_update(clicked(2, 11, data, 101))
    bot.close()
    bot = TelegramBot(settings, telegram_client=telegram.client(), agent_client=agent.client())
    bot.handle_update(clicked(3, 11, data, 101))
    bot.handle_update(incoming(4, 11, "Please continue"))
    bot.close()
    assert len(agent.submissions()) == 2
    assert observer.requests == []
    assert "uncertain" in str(telegram.bodies()[-1]["text"]).lower()


def test_expired_callback_acknowledgement_does_not_discard_a_valid_approval(tmp_path: Path) -> None:
    telegram = FakeTelegram()
    observer = FakeObserver({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor_light", "args": {"entity_id": "bulb-a"}, "id": "select"}
                ],
            ),
            AIMessage("Monitoring approved."),
        ],
        observer,
    )

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/answerCallbackQuery"):
            return httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "query too old"},
            )
        return telegram.handle(request)

    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=httpx.Client(transport=httpx.MockTransport(transport)),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Monitor this light"))
    assert observer.requests == []
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    bot.handle_update(clicked(2, 11, data, 101))
    bot.handle_update(clicked(3, 11, data, 101))
    bot.close()

    assert len(agent.submissions()) == 2
    [write] = observer.requests
    assert (write.method, write.url.path, json.loads(write.content)) == (
        "PUT",
        "/api/selection/bulb-a",
        {"selected": True},
    )
    replies = [body["text"] for body in telegram.bodies()]
    assert "Monitoring approved." in replies
    assert all("uncertain" not in reply.lower() for reply in replies)


def test_malformed_unicode_action_index_does_not_crash_or_consume_the_valid_button(
    tmp_path: Path,
) -> None:
    telegram = FakeTelegram()
    observer = FakeObserver({"/api/selection/bulb-a": snapshot("put_selection")})
    agent = GraphAegra(
        [
            AIMessage(
                "",
                tool_calls=[
                    {"name": "monitor_light", "args": {"entity_id": "bulb-a"}, "id": "select"}
                ],
            ),
            AIMessage("Monitoring approved."),
        ],
        observer,
    )
    bot = TelegramBot(
        Settings(
            bot_token=SecretStr("123:secret"),
            allowed_user_ids=frozenset({11}),
            state_path=str(tmp_path / "bot.sqlite3"),
        ),
        telegram_client=telegram.client(),
        agent_client=agent.client(),
    )
    bot.handle_update(incoming(1, 11, "Monitor this light"))
    data = telegram.bodies()[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    token, _, decision = data.split(":")
    bot.handle_update(clicked(2, 11, f"{token}:²:{decision}", 101))
    assert len(agent.submissions()) == 1
    assert observer.requests == []
    bot.handle_update(clicked(3, 11, data, 101))
    bot.close()
    assert len(agent.submissions()) == 2
    assert len(observer.requests) == 1
    assert telegram.bodies()[-1]["text"] == "Monitoring approved."
