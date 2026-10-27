"""The graph Aegra loads. Aegra supplies the Postgres checkpointer."""

from home_keeper_agent.graph import build_agent
from home_keeper_agent.keeper import KeeperClient
from home_keeper_agent.model import chat_model

agent = build_agent(chat_model(), KeeperClient.from_env())
