"""The graph Aegra loads. Aegra supplies the Postgres checkpointer."""

from home_keeper_agent.graph import build_agent
from home_keeper_agent.keeper import KeeperClient
from home_keeper_agent.model import chat_model
from home_keeper_agent.settings import AgentSettings
from home_keeper_settings import load

settings = load(AgentSettings)
agent = build_agent(chat_model(settings), KeeperClient(settings.home_keeper_url))
