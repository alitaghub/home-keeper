"""show_chart in the agent graph: who is offered it, and what the model sees when a spec fails."""

from collections.abc import Callable, Sequence
from typing import Any, override

import pytest
from agent_support import FakeKeeper, ScriptedChatModel
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from pydantic import Field

from home_keeper_agent.graph import build_agent

BAR = {
    "kind": "bar",
    "title": "Switch-ons this week",
    "unit": "times",
    "categories": ["Desk lamp", "Kettle plug"],
    "series": [{"name": "This week", "values": [14, 21]}],
}
TELEGRAM_SENDER = {"kind": "telegram", "id": 424242, "name": "Ali"}


def _tool_name(tool: dict[str, Any] | type | Callable[..., Any] | BaseTool) -> str:
    # The graph binds BaseTool instances; anything else is kept by its repr.
    return tool.name if isinstance(tool, BaseTool) else repr(tool)


class RecordingChatModel(ScriptedChatModel):
    """Keeps the tools offered to and the messages sent to each model call."""

    offered: list[set[str]] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @override
    def bind_tools(
        self,
        # The base signature takes Any-typed tool dicts and kwargs; the recorder keeps names only.
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        self.offered.append({_tool_name(tool) for tool in tools})
        return self

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


@pytest.mark.parametrize(
    ("configurable", "offered"),
    [
        pytest.param({}, True, id="browser chat, which sets no sender"),
        pytest.param({"sender": {"kind": "browser_chat"}}, True, id="browser chat sender"),
        pytest.param({"sender": TELEGRAM_SENDER}, False, id="telegram sender"),
    ],
)
def test_show_chart_is_offered_to_browser_runs_and_never_to_telegram_runs(
    configurable: dict[str, object], offered: bool
) -> None:
    model = RecordingChatModel(responses=[AIMessage("Done.")])
    agent = build_agent(model, FakeKeeper().client())
    config: RunnableConfig = {"configurable": configurable}

    agent.invoke({"messages": [HumanMessage("How long was the desk lamp on?")]}, config)  # pyright: ignore[reportUnknownMemberType] -- langgraph leaves Command's type parameter unparameterized

    [tools] = model.offered
    # The run still gets the keeper's tools; only the chart tool depends on the channel.
    assert "get_usage" in tools
    assert ("show_chart" in tools) is offered


def test_an_invalid_chart_reaches_the_model_as_an_error_and_a_corrected_one_is_shown() -> None:
    invalid = {**BAR, "color": "#ff0000"}
    model = RecordingChatModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[{"name": "show_chart", "args": {"spec": invalid}, "id": "call-bad"}],
            ),
            AIMessage(
                "", tool_calls=[{"name": "show_chart", "args": {"spec": BAR}, "id": "call-good"}]
            ),
            AIMessage("Here is the chart."),
        ]
    )
    agent = build_agent(model, FakeKeeper().client())

    state = agent.invoke({"messages": [HumanMessage("Chart the switch-ons.")]})  # pyright: ignore[reportUnknownMemberType] -- langgraph leaves Command's type parameter unparameterized

    # The run went on after the rejected spec: the model was called again, twice.
    assert len(model.seen) == 3
    [rejected] = [
        message
        for message in model.seen[1]
        if isinstance(message, ToolMessage) and message.tool_call_id == "call-bad"
    ]
    assert rejected.status == "error"
    # The model learns which field broke the spec, so it can correct it.
    assert "color" in rejected.text
    [shown] = [
        message
        for message in state["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == "call-good"
    ]
    assert shown.status == "success"
    assert shown.text
    assert "error" not in shown.text.lower()
    assert state["messages"][-1].text == "Here is the chart."
