"""HTTP fixtures for Telegram adapter tests; the agent graph remains real."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from agent_support import FakeObserver, ScriptedChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from home_observer_agent.graph import build_agent


@dataclass
class FakeTelegram:
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    next_message_id: int = 100
    bot_id: int = 12345

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/getMe"):
            return httpx.Response(
                200, json={"ok": True, "result": {"id": self.bot_id, "is_bot": True}}
            )
        if request.url.path.endswith("/getWebhookInfo"):
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        self.next_message_id += 1
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": self.next_message_id}},
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def bodies(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        # Telegram JSON is external data; tests inspect it at the HTTP boundary.
        return [
            json.loads(request.content)
            for request in self.requests
            if request.url.path.endswith("/" + method)
        ]


def incoming(
    update_id: int, user_id: int, text: str, *, kind: str = "private"
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": kind},
            "text": text,
        },
    }


def clicked(update_id: int, user_id: int, data: str, message_id: int) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id},
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": user_id, "type": "private"}},
        },
    }


class GraphAegra:
    """Expose the pinned thread/run HTTP shape around the actual checked-in agent graph."""

    def __init__(self, responses: list[BaseMessage], observer: "FakeObserver") -> None:

        self.graph = build_agent(
            ScriptedChatModel(responses=responses), observer.client(), InMemorySaver()
        )
        self.requests: list[httpx.Request] = []
        self.thread_ids: list[str] = []
        self.fail_next_run = False
        self.fail_after_next_run = False
        self.runs: list[dict[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:

        self.requests.append(request)
        path = request.url.path
        if path == "/threads" and request.method == "POST":
            body: dict[str, Any] = json.loads(request.content)
            thread_id = body["thread_id"]
            self.thread_ids.append(thread_id)
            return httpx.Response(200, json={"thread_id": thread_id, "status": "idle"})
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "threads":
            raise AssertionError(f"Unexpected Aegra request: {request.method} {path}")
        thread_id = parts[1]
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        if parts[2:] == ["runs", "wait"] and request.method == "POST":
            if self.fail_next_run:
                self.fail_next_run = False
                raise httpx.ReadTimeout("submission result unknown", request=request)
            body = json.loads(request.content)
            value = (
                Command(resume=body["command"]["resume"]) if body.get("command") else body["input"]
            )
            self.graph.invoke(value, config)  # pyright: ignore[reportUnknownMemberType] -- LangGraph leaves Command's generic unparameterized
            if self.fail_after_next_run:
                self.fail_after_next_run = False
                raise httpx.ReadTimeout("response lost after execution", request=request)
            values = dict(self.graph.get_state(config).values)
            values["messages"] = [
                message.model_dump(mode="json") if isinstance(message, BaseMessage) else message
                for message in values.get("messages", [])
            ]
            return httpx.Response(200, json=values)
        state = self.graph.get_state(config)
        if parts[2:] == ["state"]:
            return httpx.Response(
                200,
                json={
                    "values": {
                        "messages": [
                            message.model_dump(mode="json")
                            for message in state.values.get("messages", [])
                        ]
                    },
                    "next": list(state.next),
                    "tasks": [
                        {
                            "interrupts": [
                                {"id": interrupt.id, "value": interrupt.value}
                                for interrupt in state.interrupts
                            ]
                        }
                    ],
                    "interrupts": [
                        {"id": interrupt.id, "value": interrupt.value}
                        for interrupt in state.interrupts
                    ],
                    "checkpoint": {"thread_id": thread_id},
                },
            )
        if parts[2:] == ["runs"]:
            return httpx.Response(200, json=self.runs)
        if len(parts) == 2:
            return httpx.Response(
                200,
                json={
                    "thread_id": thread_id,
                    "status": "interrupted" if state.interrupts else "idle",
                },
            )
        raise AssertionError(f"Unexpected Aegra request: {request.method} {path}")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def submissions(self) -> list[httpx.Request]:
        return [
            request
            for request in self.requests
            if request.method == "POST" and request.url.path.endswith("/runs/wait")
        ]
