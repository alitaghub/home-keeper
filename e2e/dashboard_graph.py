"""Test-only deterministic model behind the real agent graph, streamed to the dashboard."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
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

# Words the proof types, and the tool each one proposes for the entity after it.
TOOLS = {
    "monitor": "monitor",
    "stop": "stop_monitoring",
    "turn_on": "turn_on",
    "turn_off": "turn_off",
}


def answer(messages: list[BaseMessage]) -> AIMessage:
    if isinstance(messages[-1], ToolMessage):
        return AIMessage("Result: " + messages[-1].text)
    questions = [message for message in messages if isinstance(message, HumanMessage)]
    text = questions[-1].text
    verb, _, entity = text.partition(" ")
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
        for index, chunk in enumerate(chunks(message)):
            if slow and index:
                await asyncio.sleep(SLOW_SECONDS)
            yield chunk


# Never registered by the production server. The agent-panel proof mounts its own Aegra config.
agent = build_agent(
    DashboardProofModel(responses=[]), KeeperClient(load(AgentSettings).home_keeper_url)
)
