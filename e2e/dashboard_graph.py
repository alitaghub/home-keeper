"""Test-only deterministic model behind the real agent graph, streamed to the dashboard."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, override
from uuid import uuid4

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from home_keeper_agent.graph import build_agent
from home_keeper_agent.keeper import KeeperClient
from home_keeper_agent.settings import AgentSettings
from home_keeper_settings import load

# A question starting with this word waits this long between the reply's two chunks.
SLOW = "slow"
SLOW_SECONDS = 4.0
CHART_PIECE_SECONDS = 0.4

# Words the proof types, and the tool each one proposes for the entity after it.
TOOLS = {
    "monitor": "monitor",
    "stop": "stop_monitoring",
    "turn_on": "turn_on",
    "turn_off": "turn_off",
}

HOURS = [f"{hour:02d}:00" for hour in range(24)]
DESK_MINUTES = [0] * 6 + [5, 30, 45, 20, 10, 5, 0, 0, 0, 10, 25, 40, 60, 60, 55, 45, 20, 5]
READING_MINUTES = [0] * 17 + [10, 20, 35, 50, 60, 40, 10]
# Words that show a chart, and the spec each one sends. `usage <id>[,<id>...]` reads real
# history for those devices over the last 7 days.
CHARTS: dict[str, dict[str, object]] = {
    "bar": {
        "kind": "bar",
        "title": "Switch-ons this week",
        "unit": "times",
        "categories": ["Desk lamp", "Reading light", "Kettle plug", "Hall"],
        "series": [
            {"name": "This week", "values": [14, 6, 21, 9]},
            {"name": "Last week", "values": [11, 8, 19, 12]},
        ],
    },
    # A series name with markup proves names render as text in every view.
    "line": {
        "kind": "line",
        "title": "Desk lamp on-time by hour",
        "unit": "min",
        "x_label": "Hour of day",
        "y_label": "Minutes on",
        "categories": HOURS,
        "series": [
            {"name": "Desk lamp", "values": DESK_MINUTES},
            {"name": "<b>Reading</b> light", "values": READING_MINUTES},
        ],
    },
    # Breaks the spec: two values for one category. The agent's schema rejects it, so the
    # tool result is an error and the panel says it cannot draw the chart.
    "badchart": {
        "kind": "bar",
        "title": "Broken",
        "categories": ["one"],
        "series": [{"name": "s", "values": [1, 2]}],
    },
}
# The model's fixed answers after a chart, so a reply never repeats a validation error.
CHART_SHOWN = "Here is the chart."
CHART_REJECTED = "That chart did not work."


def usage_spec(entity_ids: str) -> dict[str, object]:
    end = datetime.now(UTC)
    return {
        "kind": "usage_by_day",
        "entity_ids": entity_ids.split(","),
        "start": (end - timedelta(days=7)).isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
    }


def chart_call(text: str) -> dict[str, object] | None:
    verb, _, entity = text.removeprefix(f"{SLOW} ").partition(" ")
    if verb == "usage" and entity:
        return usage_spec(entity)
    return CHARTS.get(verb)


def answer(messages: list[BaseMessage]) -> AIMessage:
    last = messages[-1]
    if isinstance(last, ToolMessage) and last.name == "show_chart":
        return AIMessage(CHART_REJECTED if last.status == "error" else CHART_SHOWN)
    if isinstance(last, ToolMessage):
        return AIMessage("Result: " + last.text)
    questions = [message for message in messages if isinstance(message, HumanMessage)]
    text = questions[-1].text
    verb, _, entity = text.partition(" ")
    if (spec := chart_call(text)) is not None:
        return AIMessage(
            "",
            tool_calls=[
                {"name": "show_chart", "args": {"spec": spec}, "id": f"call-{uuid4().hex}"}
            ],
        )
    if verb in TOOLS and entity:
        return AIMessage(
            "",
            tool_calls=[
                {"name": TOOLS[verb], "args": {"entity_id": entity}, "id": f"call-{uuid4().hex}"}
            ],
        )
    return AIMessage(f"Reply: {text}")


# Like OpenAI's Responses API: the first text chunk has no id, so LangChain names it
# `lc_run--<run id>`, and the rest carry the response id.
def chunks(message: AIMessage) -> list[ChatGenerationChunk]:
    response_id = f"resp_{uuid4().hex}"
    if [call["name"] for call in message.tool_calls] == ["show_chart"]:
        call = message.tool_calls[0]
        args = json.dumps(call["args"])
        pieces = [args[i : i + 60] for i in range(0, len(args), 60)]
        return [
            ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    id=response_id,
                    tool_call_chunks=[
                        tool_call_chunk(
                            name=call["name"] if not index else None,
                            args=piece,
                            id=call["id"] if not index else None,
                            index=0,
                        )
                    ],
                    chunk_position="last" if index == len(pieces) - 1 else None,
                )
            )
            for index, piece in enumerate(pieces)
        ]
    if message.tool_calls:
        return [
            ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    id=response_id,
                    tool_call_chunks=[
                        tool_call_chunk(
                            name=call["name"],
                            args=json.dumps(call["args"]),
                            id=call["id"],
                            index=index,
                        )
                        for index, call in enumerate(message.tool_calls)
                    ],
                    chunk_position="last",
                )
            )
        ]
    text = message.text
    half = len(text) // 2
    return [
        ChatGenerationChunk(message=AIMessageChunk(content=text[:half])),
        ChatGenerationChunk(
            message=AIMessageChunk(content=text[half:], id=response_id, chunk_position="last")
        ),
    ]


class DashboardProofModel(FakeMessagesListChatModel):
    @override
    def bind_tools(
        self,
        # LangChain's tool-binding boundary accepts arbitrary framework tool schemas.
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=answer(messages))])

    @override
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        yield from chunks(answer(messages))

    @override
    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        message = answer(messages)
        slow = isinstance(messages[-1], HumanMessage) and messages[-1].text.startswith(SLOW)
        # A slow chart streams its arguments piece by piece, so the panel shows partial specs.
        pause = CHART_PIECE_SECONDS if message.tool_calls else SLOW_SECONDS
        for index, chunk in enumerate(chunks(message)):
            if slow and index:
                await asyncio.sleep(pause)
            yield chunk


# Never registered by the production server. The agent-panel proof mounts its own Aegra config.
agent = build_agent(
    DashboardProofModel(responses=[]), KeeperClient(load(AgentSettings).home_keeper_url)
)
