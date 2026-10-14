"""Fakes for the agent suites: the observer API behind httpx.MockTransport, and a scripted model."""

import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

import httpx
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from home_observer_agent.observer import ObserverClient
from home_observer_agent.tools import observer_tools

# The observer's API contract snapshots: real response bodies, so a drift in the observer's
# responses breaks the agent's parsing here instead of in a chat.
SNAPSHOTS = Path(__file__).resolve().parents[2] / "observer/tests/integration/snapshots/api"
OBSERVER_URL = "http://observer.test:8000"
READ_TOOLS = {
    "list_lights",
    "get_usage",
    "get_events",
    "get_coverage",
    "get_health",
    "list_devices",
}
# Writes change only the observer's selection and discovery, never a device.
WRITE_TOOLS = {"refresh_discovery", "monitor_light", "stop_monitoring"}


def snapshot(name: str) -> tuple[int, object]:
    """Return a recorded observer response as (status, body).

    The snapshots replace random IDs with `<uuid-N>`; put real UUIDs back so the fake observer
    serves what the real one does.
    """
    text = re.sub(
        r"<uuid-(\d+)>",
        lambda match: str(uuid.UUID(int=int(match.group(1)))),
        (SNAPSHOTS / f"{name}.json").read_text(),
    )
    recorded: dict[str, Any] = json.loads(text)
    return recorded["status"], recorded["body"]


@dataclass
class FakeObserver:
    """Serves canned responses by path and records every request the agent makes."""

    responses: dict[str, tuple[int, object]] = field(default_factory=dict[str, tuple[int, object]])
    failure: httpx.TransportError | None = None
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        status, body = self.responses.get(request.url.path, (404, {"detail": "Not Found"}))
        if isinstance(body, bytes):
            # Raw JSON text, for bodies json.dumps cannot write (such as 1e999).
            return httpx.Response(
                status, content=body, headers={"content-type": "application/json"}
            )
        return httpx.Response(status, json=body)

    def client(self) -> ObserverClient:
        return ObserverClient(OBSERVER_URL, transport=httpx.MockTransport(self.handle))

    def tools(self) -> dict[str, BaseTool]:
        return {tool.name: tool for tool in observer_tools(self.client())}

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def call(tools: dict[str, BaseTool], name: str, args: Mapping[str, object]) -> str:
    result = tools[name].invoke(dict(args))
    assert isinstance(result, str), f"{name} returned {type(result).__name__}, not text"
    return result


class ScriptedChatModel(FakeMessagesListChatModel):
    """Replays scripted AI messages in order; the agent binds tools, which a script ignores."""

    @override
    def bind_tools(
        self,
        # The base signature takes Any-typed tool dicts and kwargs; a script ignores both.
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self
