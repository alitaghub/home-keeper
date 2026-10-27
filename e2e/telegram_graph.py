"""Test-only deterministic model behind the real agent graph and Aegra HTTP server."""

from collections.abc import Callable, Sequence
from typing import Any, override

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from home_keeper_agent.graph import build_agent
from home_keeper_agent.keeper import KeeperClient


class TelegramProofModel(FakeMessagesListChatModel):
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
        # The model API is the boundary; all fixture replies are deterministic text/tool calls.
        return ChatResult(generations=[ChatGeneration(message=self.answer(messages))])

    def answer(self, messages: list[BaseMessage]) -> AIMessage:
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("Result: " + messages[-1].text)
        questions = [message for message in messages if isinstance(message, HumanMessage)]
        text = questions[-1].text
        verb, _, entity = text.partition(" ")
        if verb in {"monitor", "stop"} and entity:
            name = "monitor_light" if verb == "monitor" else "stop_monitoring"
            return AIMessage(
                "",
                tool_calls=[
                    {"name": name, "args": {"entity_id": entity}, "id": f"call-{len(questions)}"}
                ],
            )
        if verb in {"turn_on", "turn_off"} and entity:
            return AIMessage(
                "",
                tool_calls=[
                    {
                        "name": f"{verb}_light",
                        "args": {"entity_id": entity},
                        "id": f"call-{len(questions)}",
                    }
                ],
            )
        return AIMessage(f"Reply {len(questions)}: {text}")


# Never registered by the production server. The smoke mounts its own Aegra config.
agent = build_agent(TelegramProofModel(responses=[]), KeeperClient.from_env())
