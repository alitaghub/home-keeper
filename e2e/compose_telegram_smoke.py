"""Exercise the actual Telegram worker and pinned Aegra without Telegram or model credentials."""

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field, JsonValue, TypeAdapter

REPO = Path(__file__).resolve().parents[1]
PROJECT = "observer-telegram-proof"
FAKE_PORT = 18312
OBSERVER_PORT = 18003
AGENT_PORT = 18227
CHAT_PORT = 18311
FAKE_URL = f"http://127.0.0.1:{FAKE_PORT}"
OBSERVER_URL = f"http://127.0.0.1:{OBSERVER_PORT}"
AGENT_URL = f"http://127.0.0.1:{AGENT_PORT}"
TIMEOUT = 90
TOKEN = "123456:synthetic-telegram-token"


class Button(BaseModel):
    text: str
    callback_data: str


class Markup(BaseModel):
    inline_keyboard: list[list[Button]] = Field(default_factory=list)


class SentMessage(BaseModel):
    chat_id: int
    message_id: int
    text: str
    reply_markup: Markup = Field(default_factory=Markup)


class Selection(BaseModel):
    entity_ids: list[str]


class ComposeVolume(BaseModel):
    source: str


class ComposeService(BaseModel):
    ports: list[JsonValue] = Field(default_factory=list)
    networks: dict[str, JsonValue] = Field(default_factory=dict)
    environment: dict[str, str | None] = Field(default_factory=dict)
    volumes: list[ComposeVolume] = Field(default_factory=list)
    restart: str = ""


class ComposeConfig(BaseModel):
    services: dict[str, ComposeService]


class ReplayStatus(BaseModel):
    delivered: int = Field(ge=0)


MESSAGES = TypeAdapter(list[SentMessage])


def wait_until(description: str, predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


def sent(client: httpx.Client) -> list[SentMessage]:
    return MESSAGES.validate_json(client.get("/sent").raise_for_status().content)


def selection(client: httpx.Client) -> list[str]:
    response = client.get("/api/selection").raise_for_status()
    return Selection.model_validate_json(response.content).entity_ids


def wait_text(client: httpx.Client, text: str, user: int = 101) -> SentMessage:
    def matches() -> list[SentMessage]:
        return [
            message for message in sent(client) if message.chat_id == user and text in message.text
        ]

    wait_until(text, lambda: bool(matches()))
    return matches()[-1]


def post_text(
    client: httpx.Client, update_id: int, text: str, user: int = 101, *, replay: bool = False
) -> None:
    client.post(
        "/replay" if replay else "/updates",
        json={
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "from": {"id": user, "is_bot": False, "first_name": "Family"},
                "chat": {"id": user, "type": "private"},
                "text": text,
            },
        },
    ).raise_for_status()


def click(
    client: httpx.Client,
    update_id: int,
    message: SentMessage,
    decision: Literal["Approve", "Reject"],
) -> None:
    button = next(
        button
        for row in message.reply_markup.inline_keyboard
        for button in row
        if button.text == decision
    )
    client.post(
        "/updates",
        json={
            "update_id": update_id,
            "callback_query": {
                "id": f"callback-{update_id}",
                "from": {"id": message.chat_id, "is_bot": False, "first_name": "Family"},
                "chat_instance": "fixture",
                "message": {
                    "message_id": message.message_id,
                    "chat": {"id": message.chat_id, "type": "private"},
                },
                "data": button.callback_data,
            },
        },
    ).raise_for_status()


def check_profiles() -> None:
    for file in ("compose.yaml", "compose.demo.yaml"):
        for profiles in (
            [],
            ["--profile", "agent"],
            ["--profile", "agent", "--profile", "telegram"],
        ):
            result = subprocess.run(
                ["docker", "compose", "-f", file, *profiles, "config", "--format", "json"],
                cwd=REPO,
                check=True,
                capture_output=True,
                text=True,
            )
            config = ComposeConfig.model_validate_json(result.stdout)
            services = config.services
            assert ("telegram" in services) == ("telegram" in profiles)
            if "telegram" in services:
                bot = services["telegram"]
                assert not bot.ports
                assert set(bot.networks) == {"default"}
                assert set(bot.environment) == {
                    "TELEGRAM_BOT_TOKEN",
                    "TELEGRAM_ALLOWED_USER_IDS",
                }
                assert [volume.source for volume in bot.volumes] == ["telegram-state"]
                assert bot.restart == "unless-stopped"


def main() -> None:
    started = time.monotonic()
    check_profiles()
    with tempfile.TemporaryDirectory(prefix="observer-telegram-proof-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        config = root / "aegra.json"
        config.write_text(json.dumps({"graphs": {"agent": "/fixture/telegram_graph.py:agent"}}))
        config.chmod(0o444)
        override = root / "compose.yaml"
        override.write_text(f"""services:
  agent:
    command: [aegra, serve, --host, 0.0.0.0, --port, '2026', --config, /app/telegram-proof.json]
    volumes:
      - {config}:/app/telegram-proof.json:ro
      - {REPO / "e2e/telegram_graph.py"}:/fixture/telegram_graph.py:ro
    environment:
      PYTHONPATH: /fixture
  telegram:
    environment:
      TELEGRAM_BOT_TOKEN: {TOKEN}
      TELEGRAM_ALLOWED_USER_IDS: '101,202'
      TELEGRAM_API_URL: http://fake-telegram:8080
    depends_on:
      fake-telegram:
        condition: service_healthy
  fake-telegram:
    build:
      context: {REPO}
      dockerfile: apps/agent/Dockerfile
    command: [python, /fixture/fake_telegram.py]
    volumes:
      - {REPO / "e2e/fake_telegram.py"}:/fixture/fake_telegram.py:ro
    ports: ['127.0.0.1:{FAKE_PORT}:8080']
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"]
      interval: 1s
      timeout: 3s
      retries: 30
""")
        env = os.environ | {
            "OBSERVER_PORT": str(OBSERVER_PORT),
            "AGENT_PORT": str(AGENT_PORT),
            "CHAT_PORT": str(CHAT_PORT),
        }
        command = [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(REPO / "compose.demo.yaml"),
            "-f",
            str(override),
            "--profile",
            "agent",
            "--profile",
            "telegram",
        ]

        def compose(*args: str, check: bool = True) -> str:
            result = subprocess.run(
                command + list(args),
                cwd=REPO,
                env=env,
                check=check,
                capture_output=True,
                text=True,
                timeout=600,
            )
            return result.stdout + result.stderr

        try:
            compose("up", "--build", "--wait")
            with httpx.Client(base_url=FAKE_URL, timeout=10, trust_env=False) as client:
                post_text(client, 1, "hello")
                wait_text(client, "Reply 1: hello")
                post_text(client, 2, "follow-up")
                wait_text(client, "Reply 2: follow-up")
                post_text(client, 3, "my question", user=202)
                wait_text(client, "Reply 1: my question", user=202)
                post_text(client, 4, "monitor light.hue_desk")
                prompt = wait_text(client, "light.hue_desk?")
                with httpx.Client(base_url=OBSERVER_URL, timeout=10, trust_env=False) as observer:
                    assert selection(observer) == []
                    compose("restart", "telegram", "agent")
                    wait_until(
                        "agent healthy after restart",
                        lambda: (
                            httpx.get(f"{AGENT_URL}/health", timeout=3, trust_env=False).is_success
                        ),
                    )
                    # The graph and bot state both persist. The old prompt is still valid.
                    click(client, 5, prompt, "Approve")
                    wait_until(
                        "approved selection",
                        lambda: selection(observer) == ["light.hue_desk"],
                    )
                    wait_text(client, "Result:")
                    post_text(client, 6, "stop light.hue_desk")
                    stop_prompt = wait_text(client, "Stop monitoring light.hue_desk?")
                    click(client, 7, stop_prompt, "Reject")
                    # Rejection must finish, then leave monitoring selected.
                    wait_until(
                        "rejection reply",
                        lambda: (
                            len(
                                [
                                    message
                                    for message in sent(client)
                                    if message.text.startswith("Result:")
                                ]
                            )
                            == 2
                        ),
                    )
                    assert selection(observer) == ["light.hue_desk"]
                    click(client, 8, stop_prompt, "Approve")
                    wait_text(client, "no longer available")
                    assert selection(observer) == ["light.hue_desk"]
                post_text(client, 9, "/new")
                wait_text(client, "fresh conversation")
                post_text(client, 10, "fresh")
                wait_text(client, "Reply 1: fresh")
                compose("restart", "telegram")
                post_text(client, 11, "after restart")
                wait_text(client, "Reply 2: after restart")
                # Redelivery cannot start another run after the bot reopens its state file.
                post_text(client, 11, "after restart", replay=True)
                wait_until(
                    "stale update delivered despite the polling offset",
                    lambda: (
                        ReplayStatus.model_validate_json(
                            client.get("/replay").raise_for_status().content
                        ).delivered
                        == 1
                    ),
                )
                post_text(client, 12, "last")
                wait_text(client, "Reply 3: last")
                assert (
                    len(
                        [
                            message
                            for message in sent(client)
                            if message.text == "Reply 2: after restart"
                        ]
                    )
                    == 1
                )
            logs = compose("logs", "--no-color", "telegram")
            assert TOKEN not in logs
            print(
                json.dumps(
                    {
                        "telegram_compose": "passed",
                        "seconds": round(time.monotonic() - started, 2),
                        "proved": "real bot and pinned agent HTTP, private family threads, "
                        "follow-ups, approvals/rejection, pending restart, stale button, "
                        "reset, bot restart, redelivery, no token logs",
                    }
                )
            )
        except BaseException:
            # Preserve useful container diagnostics on failure, not synthetic request URLs.
            (REPO / "telegram-compose-logs.txt").write_text(
                compose("logs", "--no-color", check=False).replace(TOKEN, "<redacted>")
            )
            raise
        finally:
            compose("down", "--volumes", check=False)


if __name__ == "__main__":
    main()
