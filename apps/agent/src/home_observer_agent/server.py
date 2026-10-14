"""The graph Aegra loads. Aegra supplies the Postgres checkpointer."""

from home_observer_agent.graph import build_agent
from home_observer_agent.model import chat_model
from home_observer_agent.observer import ObserverClient

agent = build_agent(chat_model(), ObserverClient.from_env())
