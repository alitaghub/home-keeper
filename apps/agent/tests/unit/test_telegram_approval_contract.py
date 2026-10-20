from typing import get_args

import pytest
from agent_support import FakeObserver, ScriptedChatModel
from langchain_core.messages import AIMessage

from home_observer_agent import graph
from home_observer_agent.telegram import Action


def test_telegram_action_names_match_the_graph_actions_requiring_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(graph, "create_deep_agent", capture)
    graph.build_agent(ScriptedChatModel(responses=[AIMessage("Unused")]), FakeObserver().client())
    approval_config = captured["interrupt_on"]
    assert approval_config == dict.fromkeys(
        get_args(Action.model_fields["name"].annotation), graph.APPROVE_OR_REJECT
    )
