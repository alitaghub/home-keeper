"""Private Telegram transport for the existing Aegra agent.

One synchronous worker owns the token. SQLite stores identity and delivery state;
Aegra remains the only owner of conversations and tool execution.
"""

import logging
import os
import secrets
import sqlite3
import time
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

CONFIGURATION_ERROR = (
    "Set TELEGRAM_BOT_TOKEN and a comma-separated TELEGRAM_ALLOWED_USER_IDS "
    "containing positive numeric user IDs."
)


class SettingsError(ValueError):
    """Fixed configuration diagnostic that never includes supplied values."""


class Settings(BaseModel):
    bot_token: SecretStr
    allowed_user_ids: frozenset[int] = Field(min_length=1)
    agent_url: str = "http://agent:2026"
    state_path: str = "/state/telegram.sqlite3"
    api_url: str = "https://api.telegram.org"

    @field_validator("bot_token")
    @classmethod
    def token_required(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("TELEGRAM_BOT_TOKEN must not be empty.")
        return SecretStr(value.get_secret_value().strip())

    @field_validator("allowed_user_ids")
    @classmethod
    def positive_ids(cls, value: frozenset[int]) -> frozenset[int]:
        if any(user_id <= 0 for user_id in value):
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain positive numeric IDs.")
        return value

    @classmethod
    def from_env(cls) -> Self:
        try:
            values: dict[str, object] = {
                "bot_token": os.environ["TELEGRAM_BOT_TOKEN"],
                "allowed_user_ids": [
                    value.strip() for value in os.environ["TELEGRAM_ALLOWED_USER_IDS"].split(",")
                ],
            }
            for field, variable in (
                ("agent_url", "TELEGRAM_AGENT_URL"),
                ("state_path", "TELEGRAM_STATE_PATH"),
                ("api_url", "TELEGRAM_API_URL"),
            ):
                if variable in os.environ:
                    values[field] = os.environ[variable]
            return cls.model_validate(values)
        except (KeyError, ValueError):
            raise SettingsError(CONFIGURATION_ERROR) from None


class Wire(BaseModel):
    model_config = ConfigDict(strict=True)


class User(Wire):
    id: int = Field(gt=0)


class Chat(Wire):
    id: int
    type: str


class Message(Wire):
    message_id: int
    chat: Chat
    sender: User | None = Field(default=None, alias="from")
    text: str | None = None


class Callback(Wire):
    id: str
    sender: User = Field(alias="from")
    message: Message | None = None
    data: str = ""


class UpdatePosition(Wire):
    update_id: int


class Update(UpdatePosition):
    message: Message | None = None
    callback_query: Callback | None = None


class TelegramResult(Wire):
    ok: bool
    result: JsonValue = None
    error_code: int | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class WebhookInfo(Wire):
    url: str


class SentMessage(Wire):
    message_id: int = Field(gt=0)


class AgentMessage(Wire):
    id: str | None = None
    type: str = ""
    role: str = ""
    content: JsonValue = None
    tool_calls: list[JsonValue] = Field(default_factory=list)


class Values(Wire):
    messages: list[AgentMessage] = Field(default_factory=list)


class ActionArguments(Wire):
    model_config = ConfigDict(strict=True, extra="forbid")

    entity_id: str


class Action(Wire):
    name: Literal["monitor_light", "stop_monitoring"]
    args: ActionArguments
    description: str = ""


class Review(Wire):
    action_name: str
    allowed_decisions: list[str]


class Approval(Wire):
    action_requests: list[Action] = Field(min_length=1)
    review_configs: list[Review]

    @model_validator(mode="after")
    def consistent_reviews(self) -> Self:
        if len(self.action_requests) != len(self.review_configs):
            raise ValueError("Every action requires a matching review configuration.")
        for action, review in zip(self.action_requests, self.review_configs, strict=True):
            if action.name != review.action_name or not {"approve", "reject"} <= set(
                review.allowed_decisions
            ):
                raise ValueError("Every action must support approval and rejection.")
        return self


class Interrupt(Wire):
    id: str
    value: JsonValue


class Task(Wire):
    interrupts: list[Interrupt] = Field(default_factory=list)


class State(Wire):
    values: Values = Field(default_factory=Values)
    tasks: list[Task] = Field(default_factory=list)
    next: list[str] = Field(default_factory=list)


class Run(Wire):
    status: str


type Decision = Literal["approve", "reject"]
DECISION = TypeAdapter(Decision)


class Pending(BaseModel):
    token: str
    interrupt_id: str
    approval: Approval
    message_ids: list[int] = Field(default_factory=list)
    decisions: list[Decision | None]


class IdleConversation(BaseModel):
    thread_id: str
    status: Literal["idle"] = "idle"
    pending: None = None


class SubmittingConversation(BaseModel):
    thread_id: str
    status: Literal["submitting"] = "submitting"


class PendingConversation(BaseModel):
    thread_id: str
    status: Literal["pending"] = "pending"
    pending: Pending


type Conversation = Annotated[
    IdleConversation | SubmittingConversation | PendingConversation, Field(discriminator="status")
]
CONVERSATION = TypeAdapter(Conversation)


class TransportError(Exception):
    """A fixed diagnostic safe to show without an HTTP request URL."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PollError(TransportError):
    def __init__(
        self,
        message: str,
        *,
        retry_after: int = 1,
        fatal: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after
        self.fatal = fatal


HELP = (
    "Ask a text question about your home. Monitoring changes need Approve or Reject. "
    "Use /new to start a fresh conversation; old history is kept."
)
UNCERTAIN = (
    "The agent's result is uncertain. I will not repeat the request. "
    "Try a new question once it has finished, or use /new."
)
FAILURE = "The agent could not answer. Please try a new question or use /new."
REMINDER = "Choose Approve or Reject on the pending request, or use /new."
STALE = "This approval is no longer available. Use the current buttons or /new."
UNSUPPORTED_APPROVAL = "Unsupported approval request. Use /new or browser chat."
MESSAGE_LIMIT = 4096


def select_final_answer(before: Values, current: Values) -> str:
    old_ids = {message.id for message in before.messages if message.id is not None}
    candidates = [
        message
        for index, message in enumerate(current.messages)
        if (message.type == "ai" or message.role == "assistant")
        and not message.tool_calls
        and (
            (message.id is not None and message.id not in old_ids)
            or (message.id is None and index >= len(before.messages))
        )
    ]
    text = ""
    if candidates:
        content = candidates[-1].content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            blocks: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    value = block.get("text")
                    if isinstance(value, str):
                        blocks.append(value)
            text = "\n".join(blocks)
    return text if text.strip() else FAILURE


def match_callback_reference(
    callback: Callback, conversation: PendingConversation
) -> tuple[int, Decision] | None:
    parts = callback.data.split(":")
    pending = conversation.pending
    if (
        callback.message is None or len(parts) != 3  # noqa: PLR2004 -- token:index:decision is the wire format
    ):
        return None
    token, raw_index, raw_decision = parts
    try:
        decision = DECISION.validate_python(raw_decision)
    except ValidationError:
        return None
    if (
        token != pending.token
        or not raw_index.isascii()
        or not raw_index.isdigit()
        or len(raw_index) > len(str(len(pending.decisions)))
    ):
        return None
    # Bound ASCII digits before conversion: Unicode digits and huge integers can raise.
    index = int(raw_index)
    if (
        callback.message.chat.id != callback.sender.id
        or index >= len(pending.decisions)
        or index >= len(pending.message_ids)
        or pending.message_ids[index] != callback.message.message_id
        or pending.decisions[index] is not None
    ):
        return None
    return index, decision


class TelegramBot:
    def __init__(
        self,
        settings: Settings,
        *,
        telegram_client: httpx.Client | None = None,
        agent_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.telegram = telegram_client or httpx.Client(timeout=40)
        self.agent = agent_client or httpx.Client(timeout=300)
        if settings.state_path != ":memory:":
            Path(settings.state_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.state_path)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS progress "
            "(id INTEGER PRIMARY KEY CHECK(id=1), offset INTEGER NOT NULL);"
            "INSERT OR IGNORE INTO progress VALUES (1, 0);"
            "CREATE TABLE IF NOT EXISTS bot_owner "
            "(id INTEGER PRIMARY KEY CHECK(id=1), bot_id INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS conversations "
            "(user_id INTEGER PRIMARY KEY, state TEXT NOT NULL);"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
        self.telegram.close()
        self.agent.close()

    def _telegram(
        self, method: str, body: dict[str, JsonValue], *, retry_rate_limit: bool = True
    ) -> JsonValue:
        token = self.settings.bot_token.get_secret_value()
        url = f"{self.settings.api_url.rstrip('/')}/bot{token}/{method}"
        try:
            response = self.telegram.post(url, json=body)
            result = TelegramResult.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError, ValueError):
            raise PollError("Telegram request failed; delivery may be uncertain.") from None
        if not result.ok or not response.is_success:
            if result.error_code in (401, 404):
                raise PollError(
                    "Telegram rejected the bot token.",
                    fatal=True,
                    status_code=result.error_code,
                )
            if result.error_code == HTTPStatus.CONFLICT:
                raise PollError(
                    "Telegram polling conflict: remove the webhook or stop the other bot worker.",
                    fatal=True,
                    status_code=result.error_code,
                )
            retry = result.parameters.get("retry_after", 1)
            delay = retry if type(retry) is int and retry > 0 else 1
            if result.error_code == HTTPStatus.TOO_MANY_REQUESTS and retry_rate_limit:
                # A known rejection is safe to retry; uncertain deliveries never are.
                time.sleep(delay)
                return self._telegram(method, body, retry_rate_limit=False)
            raise PollError(
                "Telegram request failed.",
                retry_after=delay,
                status_code=result.error_code or response.status_code,
            )
        return result.result

    def _agent(self, method: str, path: str, body: dict[str, JsonValue] | None = None) -> JsonValue:
        try:
            response = self.agent.request(
                method, self.settings.agent_url.rstrip("/") + path, json=body
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise TransportError(
                "Agent request failed; its outcome may be uncertain.",
                status_code=exc.response.status_code,
            ) from None
        except (httpx.HTTPError, ValueError):
            raise TransportError("Agent request failed; its outcome may be uncertain.") from None

    def validate_startup(self) -> None:
        try:
            info = WebhookInfo.model_validate(self._telegram("getWebhookInfo", {}))
        except ValidationError:
            raise TransportError("Telegram returned invalid webhook information.") from None
        if info.url:
            raise TransportError(
                "A Telegram webhook is configured. Remove it with deleteWebhook "
                "before starting this polling bot."
            )
        try:
            bot_user = User.model_validate(self._telegram("getMe", {}))
        except ValidationError:
            raise TransportError("Telegram returned an invalid bot identity from getMe.") from None
        owner = self.db.execute("SELECT bot_id FROM bot_owner WHERE id=1").fetchone()
        if owner and owner[0] != bot_user.id:
            raise TransportError(
                "This Telegram state volume belongs to a different bot. "
                "Restore that bot's token or configure a separate state volume for this bot."
            )
        self.db.execute("INSERT OR IGNORE INTO bot_owner VALUES (1, ?)", (bot_user.id,))
        self.db.commit()

    def _load(self, user_id: int) -> Conversation | None:
        row = self.db.execute(
            "SELECT state FROM conversations WHERE user_id=?", (user_id,)
        ).fetchone()
        return CONVERSATION.validate_json(row[0]) if row else None

    def _save(self, user_id: int, conversation: Conversation) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO conversations VALUES (?, ?)",
            (user_id, conversation.model_dump_json()),
        )
        self.db.commit()

    def _send(self, chat_id: int, text: str, buttons: JsonValue = None) -> int:
        result: JsonValue = None
        for start in range(0, len(text), MESSAGE_LIMIT):
            body: dict[str, JsonValue] = {
                "chat_id": chat_id,
                "text": text[start : start + MESSAGE_LIMIT],
            }
            if buttons is not None and start + MESSAGE_LIMIT >= len(text):
                body["reply_markup"] = {"inline_keyboard": buttons}
            result = self._telegram("sendMessage", body)
        try:
            return SentMessage.model_validate(result).message_id
        except ValidationError:
            raise TransportError("Telegram returned an invalid sent-message reference.") from None

    def _state(self, conversation: Conversation) -> State:
        return State.model_validate(self._agent("GET", f"/threads/{conversation.thread_id}/state"))

    def handle_update(self, update: object) -> None:
        try:
            position = UpdatePosition.model_validate(update)
        except ValidationError:
            return
        offset = self.db.execute("SELECT offset FROM progress WHERE id=1").fetchone()[0]
        if position.update_id < offset:
            return
        # A redelivered update must never resubmit an uncertain agent mutation.
        self.db.execute("UPDATE progress SET offset=? WHERE id=1", (position.update_id + 1,))
        self.db.commit()
        try:
            parsed = Update.model_validate(update)
        except ValidationError:
            return
        try:
            if parsed.callback_query is not None:
                self._callback(parsed.callback_query)
            elif parsed.message is not None:
                self._message(parsed.message)
        except (TransportError, ValidationError) as exc:
            if isinstance(exc, PollError) and exc.fatal:
                raise
            logging.warning(
                "Telegram update handling failed: operation=%s exception=%s status=%s.",
                "callback" if parsed.callback_query else "message",
                type(exc).__name__,
                exc.status_code if isinstance(exc, TransportError) else None,
            )
            message = parsed.message or (
                parsed.callback_query.message if parsed.callback_query else None
            )
            if message and message.chat.type == "private":
                sender = parsed.callback_query.sender if parsed.callback_query else message.sender
                if sender and sender.id in self.settings.allowed_user_ids:
                    try:
                        self._send(message.chat.id, UNCERTAIN)
                    except PollError as delivery_error:
                        if delivery_error.fatal:
                            raise
                    except TransportError:
                        pass

    def _message(self, message: Message) -> None:  # noqa: PLR0911 -- commands exit before agent submission
        if message.chat.type != "private" or message.sender is None:
            return
        user_id = message.sender.id
        words = (message.text or "").split(maxsplit=1)
        command = words[0] if words else ""
        if user_id not in self.settings.allowed_user_ids:
            if command == "/start":
                self._send(message.chat.id, f"Access denied. Your Telegram user ID is {user_id}.")
            return
        if command in ("/start", "/help"):
            self._send(message.chat.id, HELP)
            return
        if not message.text or not message.text.strip():
            self._send(
                message.chat.id, "Please send a text question; attachments are not supported."
            )
            return
        if command == "/new":
            self._new_conversation(user_id)
            self._send(message.chat.id, "Started a fresh conversation. Old history is kept.")
            return
        conversation = self._load(user_id)
        if conversation is None:
            conversation = self._new_conversation(user_id)
        if conversation.status == "submitting":
            runs = self._agent("GET", f"/threads/{conversation.thread_id}/runs")
            if not isinstance(runs, list) or any(
                Run.model_validate(run).status in ("pending", "running") for run in runs
            ):
                self._send(message.chat.id, UNCERTAIN)
                return
            state = self._state(conversation)
            if self._interrupts(state):
                self._send(message.chat.id, UNCERTAIN)
                return
            conversation = IdleConversation(thread_id=conversation.thread_id)
            self._save(user_id, conversation)
        if conversation.status == "pending":
            self._send(message.chat.id, REMINDER)
            return
        before = self._state(conversation)
        self._submit(
            user_id,
            message.chat.id,
            conversation,
            before,
            {"input": {"messages": [{"role": "user", "content": message.text}]}},
        )

    def _new_conversation(self, user_id: int) -> IdleConversation:
        conversation = IdleConversation(thread_id=str(uuid4()))
        self._agent("POST", "/threads", {"thread_id": conversation.thread_id})
        self._save(user_id, conversation)
        return conversation

    def _submit(
        self,
        user_id: int,
        chat_id: int,
        conversation: Conversation,
        before: State,
        request: dict[str, JsonValue],
    ) -> None:
        conversation = SubmittingConversation(thread_id=conversation.thread_id)
        # Persist intent before either a new run or an approval can reach the agent.
        self._save(user_id, conversation)
        run_output = self._agent(
            "POST",
            f"/threads/{conversation.thread_id}/runs/wait",
            {"assistant_id": "agent", **request},
        )
        self._finish(user_id, chat_id, conversation, before, run_output)

    @staticmethod
    def _interrupts(state: State) -> list[Interrupt]:
        return [interrupt for task in state.tasks for interrupt in task.interrupts]

    def _finish(
        self,
        user_id: int,
        chat_id: int,
        conversation: Conversation,
        before: State,
        run_output: JsonValue,
    ) -> None:
        state = self._state(conversation)
        interrupts = self._interrupts(state)
        if interrupts:
            if len(interrupts) != 1:
                self._send(chat_id, UNSUPPORTED_APPROVAL)
                return
            interrupt = interrupts[0]
            try:
                approval = Approval.model_validate(interrupt.value)
            except ValidationError:
                self._send(chat_id, UNSUPPORTED_APPROVAL)
                return
            pending = Pending(
                token=secrets.token_hex(12),
                interrupt_id=interrupt.id,
                approval=approval,
                decisions=[None] * len(approval.action_requests),
            )
            conversation = PendingConversation(thread_id=conversation.thread_id, pending=pending)
            self._save(user_id, conversation)
            for index, action in enumerate(approval.action_requests):
                verb = "Start" if action.name == "monitor_light" else "Stop"
                text = f"{verb} monitoring {action.args.entity_id}?"
                if action.description:
                    text += "\n" + action.description
                buttons: JsonValue = [
                    [
                        {"text": "Approve", "callback_data": f"{pending.token}:{index}:approve"},
                        {"text": "Reject", "callback_data": f"{pending.token}:{index}:reject"},
                    ]
                ]
                pending.message_ids.append(self._send(chat_id, text, buttons))
                self._save(user_id, conversation)
            return
        conversation = IdleConversation(thread_id=conversation.thread_id)
        self._save(user_id, conversation)
        self._send(chat_id, select_final_answer(before.values, Values.model_validate(run_output)))

    def _callback(self, callback: Callback) -> None:
        try:
            self._telegram("answerCallbackQuery", {"callback_query_id": callback.id})
        except PollError as exc:
            # Telegram expires acknowledgements independently of saved approval state.
            if exc.fatal:
                raise
        message = callback.message
        if (
            message is None
            or message.chat.type != "private"
            or callback.sender.id not in self.settings.allowed_user_ids
        ):
            return
        conversation = self._load(callback.sender.id)
        if not isinstance(conversation, PendingConversation):
            self._send(message.chat.id, STALE)
            return
        reference = match_callback_reference(callback, conversation)
        if reference is None:
            self._send(message.chat.id, STALE)
            return
        index, decision = reference
        pending = conversation.pending
        before = self._state(conversation)
        interrupts = self._interrupts(before)
        if (
            len(interrupts) != 1
            or interrupts[0].id != pending.interrupt_id
            or Approval.model_validate(interrupts[0].value) != pending.approval
        ):
            self._send(message.chat.id, STALE)
            return
        pending.decisions[index] = decision
        self._save(callback.sender.id, conversation)
        try:
            self._telegram(
                "editMessageReplyMarkup",
                {
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        except PollError as delivery_error:
            if delivery_error.fatal:
                raise
        if any(value is None for value in pending.decisions):
            return
        self._submit(
            callback.sender.id,
            message.chat.id,
            conversation,
            before,
            {
                "command": {
                    "resume": {"decisions": [{"type": value} for value in pending.decisions]}
                }
            },
        )

    def poll_once(self) -> None:
        offset = self.db.execute("SELECT offset FROM progress WHERE id=1").fetchone()[0]
        updates = self._telegram(
            "getUpdates",
            {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
        )
        if not isinstance(updates, list):
            raise PollError("Telegram returned invalid updates.")
        for update in updates:
            self.handle_update(update)


def main() -> None:
    # httpx's INFO request log includes the Telegram token in its URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    bot: TelegramBot | None = None
    try:
        bot = TelegramBot(Settings.from_env())
        ready = False
        delay = 1
        while True:
            try:
                if not ready:
                    bot.validate_startup()
                    ready = True
                bot.poll_once()
                delay = 1
            except PollError as exc:
                if exc.fatal:
                    raise
                logging.warning("Telegram startup or polling unavailable; reconnecting.")
                time.sleep(max(delay, exc.retry_after))
                delay = min(delay * 2, 30)
    except TransportError as exc:
        logging.error("Telegram bot stopped: %s", exc)
        raise SystemExit(1) from None
    except SettingsError:
        logging.error("Telegram bot stopped: %s", CONFIGURATION_ERROR)
        raise SystemExit(1) from None
    except (sqlite3.Error, OSError, ValidationError):
        logging.error(
            "Telegram bot stopped: cannot open or read TELEGRAM_STATE_PATH. "
            "Check the state volume, write permissions, and saved-state integrity."
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    finally:
        if bot is not None:
            bot.close()


if __name__ == "__main__":
    main()
