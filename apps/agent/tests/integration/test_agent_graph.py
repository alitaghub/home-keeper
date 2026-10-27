import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol, override

import httpx
import pytest
from agent_support import (
    READ_TOOLS,
    WRITE_TOOLS,
    FakeKeeper,
    ScriptedChatModel,
    command_requests,
    command_response,
    control_response,
    lights_response,
    snapshot,
)
from langchain.agents.middleware.human_in_the_loop import HITLRequest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, StateSnapshot
from pydantic import Field

from home_keeper_agent.graph import build_agent

THREAD: RunnableConfig = {"configurable": {"thread_id": "selection"}}
CONTROL_TOOLS = {"turn_on_light", "turn_off_light"}


def test_agent_registers_the_keeper_tools_and_no_shell_or_subagent() -> None:
    agent = build_agent(ScriptedChatModel(responses=[AIMessage("unused")]), FakeKeeper().client())

    tool_node = agent.nodes["tools"].bound
    assert isinstance(tool_node, ToolNode)
    names = set(tool_node.tools_by_name)
    assert names >= READ_TOOLS | WRITE_TOOLS | CONTROL_TOOLS
    assert not names & {"execute", "task"}


def test_scripted_tool_calls_reach_the_keeper_and_the_final_messages() -> None:
    keeper = FakeKeeper(
        {
            "/api/monitored": snapshot("monitored"),
            "/api/health": snapshot("health"),
            "/api/usage": snapshot("usage"),
        }
    )
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {"name": "list_lights", "args": {}, "id": "call-lights"},
                    {"name": "get_health", "args": {}, "id": "call-health"},
                    {
                        "name": "get_usage",
                        "args": {"start": "2026-10-12T09:59:00Z", "end": "2026-10-12T10:00:10Z"},
                        "id": "call-usage",
                    },
                ],
            ),
            AIMessage("You watch two lights; collection is healthy."),
        ]
    )
    agent = build_agent(model, keeper.client())

    question = HumanMessage("Which lights do you watch, and for how long?")
    # langgraph's invoke signature leaves Command's type parameter unparameterized.
    state = agent.invoke({"messages": [question]})  # pyright: ignore[reportUnknownMemberType]

    # list_lights also reads the control state, so the model can tell whether it may propose one.
    assert sorted(keeper.paths()) == ["/api/control", "/api/health", "/api/monitored", "/api/usage"]
    results = {
        message.tool_call_id: message.text
        for message in state["messages"]
        if isinstance(message, ToolMessage)
    }
    assert "bulb-a" in results["call-lights"]
    assert "healthy" in results["call-health"]
    assert "no_evidence" in results["call-usage"]
    assert state["messages"][-1].text == "You watch two lights; collection is healthy."


def agent_after_one_call(keeper: FakeKeeper, tool_call: ToolCall):
    """Run an agent (with the checkpointer interrupts need) through one scripted tool call."""
    model = ScriptedChatModel(responses=[AIMessage("", tool_calls=[tool_call]), AIMessage("Done.")])
    agent = build_agent(model, keeper.client(), checkpointer=InMemorySaver())
    question = HumanMessage("Change what you record.")
    # langgraph's invoke signature leaves Command's type parameter unparameterized.
    agent.invoke({"messages": [question]}, THREAD)  # pyright: ignore[reportUnknownMemberType]
    return agent


@pytest.mark.parametrize(
    ("name", "selected"), [("monitor_light", True), ("stop_monitoring", False)]
)
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_selection_change_waits_for_approval_and_reject_changes_nothing(
    name: str, selected: bool, decision: str
) -> None:
    keeper = FakeKeeper({"/api/selection/bulb-a": snapshot("put_selection")})
    call: ToolCall = {"name": name, "args": {"entity_id": "bulb-a"}, "id": "call-select"}

    agent = agent_after_one_call(keeper, call)

    [pause] = agent.get_state(THREAD).interrupts
    approval: HITLRequest = pause.value
    [request] = approval["action_requests"]
    assert request["name"] == name
    assert request["args"] == {"entity_id": "bulb-a"}
    assert keeper.requests == []

    resume = Command(resume={"decisions": [{"type": decision}]})
    # langgraph's invoke signature leaves Command's type parameter unparameterized.
    agent.invoke(resume, THREAD)  # pyright: ignore[reportUnknownMemberType]

    assert agent.get_state(THREAD).interrupts == ()
    if decision == "reject":
        assert keeper.requests == []
        return
    [put] = keeper.requests
    assert (put.method, put.url.path) == ("PUT", "/api/selection/bulb-a")
    assert put.headers["X-Home-Keeper"] == "1"
    assert json.loads(put.content) == {"selected": selected}


def test_refresh_discovery_runs_without_approval() -> None:
    keeper = FakeKeeper({"/api/discovery/refresh": snapshot("discovery_refresh")})
    call: ToolCall = {"name": "refresh_discovery", "args": {}, "id": "call-refresh"}

    agent = agent_after_one_call(keeper, call)

    assert agent.get_state(THREAD).interrupts == ()
    assert [(request.method, request.url.path) for request in keeper.requests] == [
        ("POST", "/api/discovery/refresh")
    ]


class RecordingChatModel(ScriptedChatModel):
    """Keeps the system prompt of each call, so a test can see what the model was told."""

    seen: list[str] = Field(default_factory=list)

    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.extend(message.text for message in messages if message.type == "system")
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def test_model_is_told_the_current_time_on_the_async_path_aegra_uses() -> None:
    model = RecordingChatModel(responses=[AIMessage("Monday to now.")])
    agent = build_agent(model, FakeKeeper().client())

    before = datetime.now(UTC).replace(microsecond=0)
    asyncio.run(agent.ainvoke({"messages": [HumanMessage("What counts as this week?")]}))  # pyright: ignore[reportUnknownMemberType] -- langgraph leaves Command's type parameter unparameterized
    after = datetime.now(UTC)

    [system] = model.seen
    told = re.search(r"The current time is (\S+?)\.(?:\s|$)", system)
    assert told, system
    assert before <= datetime.fromisoformat(told[1]) <= after


def light_keeper(
    state: str | None = "on",
    brightness: int | None = None,
    *,
    control: str = "on",
    command: tuple[int, object] | None = None,
) -> FakeKeeper:
    """A keeper with one monitored light (bulb-a) and a command answer for it."""
    return FakeKeeper(
        {
            "/api/monitored": lights_response(state, brightness),
            "/api/control": control_response(control),
            "/api/lights/bulb-a/command": command or command_response("observed"),
        }
    )


def agent_after_light_call(
    keeper: FakeKeeper, tool_call: ToolCall, configurable: dict[str, object] | None = None
):
    """Run an agent through one scripted call, with the run's `sender` and `may_control`."""
    model = ScriptedChatModel(responses=[AIMessage("", tool_calls=[tool_call]), AIMessage("Done.")])
    agent = build_agent(model, keeper.client(), checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "light", **(configurable or {})}}
    # langgraph's invoke signature leaves Command's type parameter unparameterized.
    agent.invoke({"messages": [HumanMessage("Switch the light.")]}, config)  # pyright: ignore[reportUnknownMemberType] -- langgraph leaves Command's type parameter unparameterized
    return agent, config


def tool_result(state: StateSnapshot) -> str:
    [result] = [message for message in state.values["messages"] if isinstance(message, ToolMessage)]
    return result.text


def off_call() -> ToolCall:
    return {"name": "turn_off_light", "args": {"entity_id": "bulb-a"}, "id": "call-light"}


def on_call(brightness_percent: int | None = None) -> ToolCall:
    args: dict[str, object] = {"entity_id": "bulb-a"}
    if brightness_percent is not None:
        args["brightness_percent"] = brightness_percent
    return {"name": "turn_on_light", "args": args, "id": "call-light"}


@pytest.mark.parametrize(
    "call",
    [off_call(), on_call(), on_call(30)],
    ids=["turn_off_light", "turn_on_light", "turn_on_light-30"],
)
def test_light_command_on_a_light_that_can_change_waits_for_one_approval(call: ToolCall) -> None:
    # An "off" light gets "on" commands and an "on" light gets "off" commands.
    keeper = light_keeper("on" if call["name"] == "turn_off_light" else "off")

    agent, config = agent_after_light_call(keeper, call)

    [pause] = agent.get_state(config).interrupts
    approval: HITLRequest = pause.value
    [request] = approval["action_requests"]
    assert request["name"] == call["name"]
    assert request["args"] == call["args"]
    assert [review["allowed_decisions"] for review in approval["review_configs"]] == [
        ["approve", "reject"]
    ]
    assert command_requests(keeper) == []


NOT_ALLOWED = "Not sent: you are not allowed to control lights."
BRIGHTNESS_RANGE = "Not sent: brightness must be 1 to 100 percent."


@pytest.mark.parametrize(
    ("call", "state", "brightness", "control", "configurable", "expected"),
    [
        pytest.param(
            off_call(),
            "on",
            None,
            "on",
            {"may_control": False},
            NOT_ALLOWED,
            id="may_control-false",
        ),
        pytest.param(
            off_call(), "on", None, "off", {}, "Not sent: light control is off.", id="control-off"
        ),
        pytest.param(on_call(0), "off", None, "on", {}, BRIGHTNESS_RANGE, id="brightness-0"),
        pytest.param(on_call(101), "off", None, "on", {}, BRIGHTNESS_RANGE, id="brightness-101"),
        pytest.param(
            off_call(),
            None,
            None,
            "on",
            {},
            "Not sent: bulb-a has no known on/off state.",
            id="no-baseline",
        ),
        pytest.param(
            off_call(),
            "unavailable",
            None,
            "on",
            {},
            "Not sent: bulb-a is unavailable.",
            id="unavailable",
        ),
        pytest.param(
            on_call(),
            "unknown",
            None,
            "on",
            {},
            "Not sent: bulb-a has no known on/off state.",
            id="unknown-state",
        ),
        pytest.param(
            off_call(), "off", None, "on", {}, "Not sent: bulb-a is already off.", id="already-off"
        ),
        pytest.param(
            on_call(), "on", 200, "on", {}, "Not sent: bulb-a is already on.", id="already-on"
        ),
        pytest.param(
            on_call(30), "on", 77, "on", {}, "Not sent: bulb-a is already on.", id="already-30%"
        ),
        pytest.param(
            on_call(30), "on", 75, "on", {}, "Not sent: bulb-a is already on.", id="within-2-below"
        ),
        pytest.param(
            on_call(30), "on", 79, "on", {}, "Not sent: bulb-a is already on.", id="within-2-above"
        ),
        # may_control is checked first, then the control switch, then the brightness range.
        pytest.param(
            on_call(101), "off", None, "off", {"may_control": False}, NOT_ALLOWED, id="order-1"
        ),
        pytest.param(
            on_call(101),
            "off",
            None,
            "off",
            {},
            "Not sent: light control is off.",
            id="order-2",
        ),
    ],
)
def test_refused_light_command_shows_no_card_sends_nothing_and_says_why(  # noqa: PLR0917 -- one pytest parameter per table column
    call: ToolCall,
    state: str | None,
    brightness: int | None,
    control: str,
    configurable: dict[str, object],
    expected: str,
) -> None:
    keeper = light_keeper(state, brightness, control=control)

    agent, config = agent_after_light_call(keeper, call, configurable)

    assert agent.get_state(config).interrupts == ()
    assert tool_result(agent.get_state(config)) == expected
    assert command_requests(keeper) == []


def test_command_on_a_light_that_is_not_monitored_is_refused_with_an_offer_to_monitor() -> None:
    keeper = light_keeper()
    call: ToolCall = {"name": "turn_off_light", "args": {"entity_id": "bulb-z"}, "id": "call-z"}

    agent, config = agent_after_light_call(keeper, call)

    assert agent.get_state(config).interrupts == ()
    assert tool_result(agent.get_state(config)) == (
        "Not sent: bulb-z is not monitored. Offer to start monitoring it first."
    )
    assert command_requests(keeper) == []


def test_command_is_refused_when_the_keeper_cannot_be_reached() -> None:
    keeper = FakeKeeper(failure=httpx.ConnectError("connection refused"))

    agent, config = agent_after_light_call(keeper, off_call())

    assert agent.get_state(config).interrupts == ()
    assert tool_result(agent.get_state(config)).startswith("Keeper error:")
    assert command_requests(keeper) == []


@pytest.mark.parametrize("brightness", [74, 80])
def test_brightness_more_than_2_away_from_the_request_still_asks(brightness: int) -> None:
    # 30 percent is 77 of 255; 74 and 80 are the first values outside the +-2 band.
    keeper = light_keeper("on", brightness)

    agent, config = agent_after_light_call(keeper, on_call(30))

    assert len(agent.get_state(config).interrupts) == 1


def test_turning_on_an_on_light_to_another_brightness_asks() -> None:
    keeper = light_keeper("on", 200)

    agent, config = agent_after_light_call(keeper, on_call(30))

    assert len(agent.get_state(config).interrupts) == 1


def test_a_pre_checked_call_does_not_stop_read_tools_from_running() -> None:
    keeper = light_keeper()
    calls: list[ToolCall] = [
        {"name": "get_health", "args": {}, "id": "call-health"},
        {"name": "turn_off_light", "args": {"entity_id": "bulb-z"}, "id": "call-z"},
    ]
    model = ScriptedChatModel(responses=[AIMessage("", tool_calls=calls), AIMessage("Done.")])
    agent = build_agent(model, keeper.client(), checkpointer=InMemorySaver())

    agent.invoke({"messages": [HumanMessage("Check.")]}, THREAD)  # pyright: ignore[reportUnknownMemberType] -- langgraph leaves Command's type parameter unparameterized

    results = {
        message.tool_call_id
        for message in agent.get_state(THREAD).values["messages"]
        if isinstance(message, ToolMessage)
    }
    assert results == {"call-health", "call-z"}
    assert agent.get_state(THREAD).interrupts == ()


def test_two_light_commands_in_one_response_ask_only_for_the_one_that_can_work() -> None:
    keeper = light_keeper("on")
    calls: list[ToolCall] = [
        off_call(),
        {"name": "turn_off_light", "args": {"entity_id": "bulb-z"}, "id": "call-z"},
    ]
    model = ScriptedChatModel(responses=[AIMessage("", tool_calls=calls), AIMessage("Done.")])
    agent = build_agent(model, keeper.client(), checkpointer=InMemorySaver())

    agent.invoke({"messages": [HumanMessage("Turn both off.")]}, THREAD)  # pyright: ignore[reportUnknownMemberType] -- langgraph leaves Command's type parameter unparameterized

    [pause] = agent.get_state(THREAD).interrupts
    approval: HITLRequest = pause.value
    assert [request["args"] for request in approval["action_requests"]] == [{"entity_id": "bulb-a"}]
    resume(agent, THREAD, "reject")
    assert command_requests(keeper) == []
    results = {
        message.tool_call_id: message.text
        for message in agent.get_state(THREAD).values["messages"]
        if isinstance(message, ToolMessage)
    }
    assert results["call-z"].startswith("Not sent: bulb-z is not monitored.")


class Resumable(Protocol):
    def invoke(self, input: Command[object], config: RunnableConfig) -> object: ...


def resume(agent: Resumable, config: RunnableConfig, decision: str) -> None:
    agent.invoke(Command(resume={"decisions": [{"type": decision}]}), config)


@pytest.mark.parametrize("call", [off_call(), on_call(30)], ids=["off", "on-30"])
def test_rejected_light_command_sends_nothing(call: ToolCall) -> None:
    keeper = light_keeper("on" if call["name"] == "turn_off_light" else "off")
    agent, config = agent_after_light_call(keeper, call)

    resume(agent, config, "reject")

    assert agent.get_state(config).interrupts == ()
    assert command_requests(keeper) == []


@pytest.mark.parametrize(
    ("call", "state", "body"),
    [
        pytest.param(off_call(), "on", {"action": "off"}, id="off"),
        pytest.param(on_call(), "off", {"action": "on"}, id="on"),
        pytest.param(on_call(30), "off", {"action": "on", "brightness_pct": 30}, id="on-30"),
    ],
)
def test_approved_light_command_sends_exactly_that_command_as_the_browser_chat(
    call: ToolCall, state: str, body: dict[str, object]
) -> None:
    keeper = light_keeper(state)
    agent, config = agent_after_light_call(keeper, call)

    resume(agent, config, "approve")

    [post] = command_requests(keeper)
    assert (post.method, post.url.path) == ("POST", "/api/lights/bulb-a/command")
    assert post.headers["X-Home-Keeper"] == "1"
    assert json.loads(post.content) == {"command": body, "sender": {"kind": "browser_chat"}}


def test_approved_light_command_carries_the_senders_of_the_run() -> None:
    sender = {"kind": "telegram", "id": 424242, "name": "Ali"}
    keeper = light_keeper("on")
    agent, config = agent_after_light_call(keeper, off_call(), {"sender": sender})

    resume(agent, config, "approve")

    [post] = command_requests(keeper)
    assert json.loads(post.content)["sender"] == sender


def test_light_id_is_sent_as_one_path_segment() -> None:
    entity_id = "a/b c"
    keeper = FakeKeeper(
        {
            "/api/monitored": lights_response("on", entity_id=entity_id),
            "/api/control": control_response("on"),
            f"/api/lights/{entity_id}/command": command_response("observed"),
        }
    )
    call: ToolCall = {"name": "turn_off_light", "args": {"entity_id": entity_id}, "id": "call-id"}
    agent, config = agent_after_light_call(keeper, call)

    resume(agent, config, "approve")

    [post] = command_requests(keeper)
    assert post.url.raw_path == b"/api/lights/a%2Fb%20c/command"


@pytest.mark.parametrize(
    ("call", "state", "record", "expected"),
    [
        pytest.param(
            off_call(),
            "on",
            command_response("observed", "off"),
            "bulb-a is now off (observed).",
            id="observed-off",
        ),
        pytest.param(
            on_call(30),
            "off",
            command_response("observed", "on", 30),
            "bulb-a is now on (observed).",
            id="observed-on",
        ),
        pytest.param(
            off_call(),
            "on",
            command_response("not_observed", "off"),
            "Sent to bulb-a, but no change was seen within 5 seconds.",
            id="not_observed",
        ),
        pytest.param(
            off_call(),
            "on",
            command_response("refused", "off"),
            "Home Assistant refused the command for bulb-a.",
            id="refused",
        ),
        pytest.param(
            off_call(),
            "on",
            command_response("unreachable", "off"),
            "Cannot reach the light controller; bulb-a may not have changed.",
            id="unreachable",
        ),
    ],
)
def test_approved_light_command_reports_the_observed_outcome(
    call: ToolCall, state: str, record: tuple[int, object], expected: str
) -> None:
    keeper = light_keeper(state, command=record)
    agent, config = agent_after_light_call(keeper, call)

    resume(agent, config, "approve")

    [result] = [
        message
        for message in agent.get_state(config).values["messages"]
        if isinstance(message, ToolMessage)
    ]
    assert result.text == expected


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        ("error_already_in_state", "The light is already in that state"),
        ("error_control_off", "Light control is off"),
        ("error_unavailable", "The light is unavailable"),
    ],
)
def test_approved_light_command_the_keeper_turns_down_relays_its_reason(
    fixture: str, message: str
) -> None:
    keeper = light_keeper("on", command=snapshot(fixture))
    agent, config = agent_after_light_call(keeper, off_call())

    resume(agent, config, "approve")

    [result] = [
        message
        for message in agent.get_state(config).values["messages"]
        if isinstance(message, ToolMessage)
    ]
    assert result.text.startswith(f"Keeper error: HTTP 409: {message}")


def test_prompt_lets_the_model_propose_light_commands_only_after_the_user_asks() -> None:
    model = RecordingChatModel(responses=[AIMessage("Hello.")])
    agent = build_agent(model, FakeKeeper().client())

    asyncio.run(agent.ainvoke({"messages": [HumanMessage("Hi")]}))  # pyright: ignore[reportUnknownMemberType] -- langgraph leaves Command's type parameter unparameterized

    [system] = model.seen
    assert "cannot control devices" not in system
    assert "turn_on_light" in system
    assert "turn_off_light" in system
    assert "Control: on" in system
    # The consent rule: a command follows the user's request or agreement, never a hunch.
    assert "only after the user asks for that change or agrees to your suggestion" in system


def test_light_command_reusing_an_earlier_call_id_still_waits_for_approval() -> None:
    keeper = light_keeper("on")
    keeper.responses["/api/health"] = snapshot("health")
    model = ScriptedChatModel(
        responses=[
            AIMessage("", tool_calls=[{"name": "get_health", "args": {}, "id": "call-1"}]),
            AIMessage("", tool_calls=[{**off_call(), "id": "call-1"}]),
            AIMessage("Done."),
        ]
    )
    agent = build_agent(model, keeper.client(), checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "reuse"}}

    agent.invoke({"messages": [HumanMessage("Check health.")]}, config)  # pyright: ignore[reportUnknownMemberType] -- langgraph leaves Command's type parameter unparameterized

    assert command_requests(keeper) == []
    assert len(agent.get_state(config).interrupts) == 1


def test_permission_is_checked_again_when_an_approval_resumes() -> None:
    keeper = light_keeper("on")
    agent, config = agent_after_light_call(keeper, off_call(), {"may_control": True})
    assert len(agent.get_state(config).interrupts) == 1
    resumed: RunnableConfig = {"configurable": {"thread_id": "light", "may_control": False}}

    resume(agent, resumed, "approve")

    assert tool_result(agent.get_state(resumed)) == NOT_ALLOWED
    assert command_requests(keeper) == []
