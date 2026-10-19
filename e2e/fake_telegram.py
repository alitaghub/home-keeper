"""Telegram's HTTP boundary for the credential-free Compose proof, never a real bot."""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

from pydantic import BaseModel, Field, JsonValue, TypeAdapter

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class Poll(BaseModel):
    offset: int = 0


class Sent(BaseModel):
    chat_id: int
    text: str
    reply_markup: dict[str, JsonValue] = Field(default_factory=dict)


class TelegramHandler(BaseHTTPRequestHandler):
    updates: ClassVar[list[dict[str, JsonValue]]] = []
    sent: ClassVar[list[dict[str, JsonValue]]] = []

    def log_message(self, format: str, *args: object) -> None:
        pass  # Fixture request URLs contain the synthetic token; do not teach token logging.

    def reply(self, body: object) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/sent":
            self.reply(self.sent)
        elif self.path == "/health":
            self.reply({"ok": True})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        params = JSON_OBJECT.validate_json(self.rfile.read(size) or b"{}")
        if self.path == "/updates":
            self.updates.append(params)
            self.reply({"ok": True})
        else:
            self.method(self.path, params)

    def method(self, path: str, params: dict[str, JsonValue]) -> None:
        match path.rsplit("/", 1)[-1]:
            case "getMe":
                self.reply(
                    {
                        "ok": True,
                        "result": {
                            "id": 123,
                            "is_bot": True,
                            "first_name": "Fixture",
                            "username": "observer_fixture_bot",
                        },
                    }
                )
            case "getWebhookInfo":
                self.reply({"ok": True, "result": {"url": "", "pending_update_count": 0}})
            case "getUpdates":
                offset = Poll.model_validate(params).offset
                time.sleep(0.1)
                self.reply(
                    {
                        "ok": True,
                        "result": [
                            update
                            for update in self.updates
                            if isinstance(update_id := update.get("update_id"), int)
                            and update_id >= offset
                        ],
                    }
                )
            case "sendMessage":
                sent = Sent.model_validate(params)
                message_id = len(self.sent) + 1
                message: dict[str, JsonValue] = sent.model_dump(mode="json")
                message["message_id"] = message_id
                self.sent.append(message)
                self.reply(
                    {
                        "ok": True,
                        "result": {
                            "message_id": message_id,
                            "chat": {"id": sent.chat_id, "type": "private"},
                            "text": sent.text,
                        },
                    }
                )
            case "answerCallbackQuery" | "editMessageReplyMarkup":
                self.reply({"ok": True, "result": True})
            case _:
                self.send_error(404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), TelegramHandler).serve_forever()  # noqa: S104 -- test container has one loopback-published control port
