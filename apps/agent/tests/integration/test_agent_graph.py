import json

import pytest
from agent_support import READ_TOOLS, WRITE_TOOLS, FakeObserver, ScriptedChatModel, snapshot
from langchain.agents.middleware.human_in_the_loop import HITLRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from home_observer_agent.graph import build_agent

THREAD: RunnableConfig = {"configurable": {"thread_id": "selection"}}


def test_agent_registers_the_observer_tools_and_no_shell_or_subagent() -> None:
    agent = build_agent(ScriptedChatModel(responses=[AIMessage("unused")]), FakeObserver().client())

    tool_node = agent.nodes["tools"].bound
    assert isinstance(tool_node, ToolNode)
    names = set(tool_node.tools_by_name)
    assert names >= READ_TOOLS | WRITE_TOOLS
    assert not names & {"execute", "task"}


def test_scripted_tool_calls_reach_the_observer_and_the_final_messages() -> None:
    observer = FakeObserver(
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
    agent = build_agent(model, observer.client())

    question = HumanMessage("Which lights do you watch, and for how long?")
    # langgraph's invoke signature leaves Command's type parameter unparameterized.
    state = agent.invoke({"messages": [question]})  # pyright: ignore[reportUnknownMemberType]

    assert sorted(observer.paths()) == ["/api/health", "/api/monitored", "/api/usage"]
    results = {
        message.tool_call_id: message.text
        for message in state["messages"]
        if isinstance(message, ToolMessage)
    }
    assert "bulb-a" in results["call-lights"]
    assert "healthy" in results["call-health"]
    assert "no_evidence" in results["call-usage"]
    assert state["messages"][-1].text == "You watch two lights; collection is healthy."


def agent_after_one_call(observer: FakeObserver, tool_call: ToolCall):
    """Run an agent (with the checkpointer interrupts need) through one scripted tool call."""
    model = ScriptedChatModel(responses=[AIMessage("", tool_calls=[tool_call]), AIMessage("Done.")])
    agent = build_agent(model, observer.client(), checkpointer=InMemorySaver())
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
    observer = FakeObserver({"/api/selection/bulb-a": snapshot("put_selection")})
    call: ToolCall = {"name": name, "args": {"entity_id": "bulb-a"}, "id": "call-select"}

    agent = agent_after_one_call(observer, call)

    [pause] = agent.get_state(THREAD).interrupts
    approval: HITLRequest = pause.value
    [request] = approval["action_requests"]
    assert request["name"] == name
    assert request["args"] == {"entity_id": "bulb-a"}
    assert observer.requests == []

    resume = Command(resume={"decisions": [{"type": decision}]})
    # langgraph's invoke signature leaves Command's type parameter unparameterized.
    agent.invoke(resume, THREAD)  # pyright: ignore[reportUnknownMemberType]

    assert agent.get_state(THREAD).interrupts == ()
    if decision == "reject":
        assert observer.requests == []
        return
    [put] = observer.requests
    assert (put.method, put.url.path) == ("PUT", "/api/selection/bulb-a")
    assert put.headers["X-Home-Observer"] == "1"
    assert json.loads(put.content) == {"selected": selected}


def test_refresh_discovery_runs_without_approval() -> None:
    observer = FakeObserver({"/api/discovery/refresh": snapshot("discovery_refresh")})
    call: ToolCall = {"name": "refresh_discovery", "args": {}, "id": "call-refresh"}

    agent = agent_after_one_call(observer, call)

    assert agent.get_state(THREAD).interrupts == ()
    assert [(request.method, request.url.path) for request in observer.requests] == [
        ("POST", "/api/discovery/refresh")
    ]
