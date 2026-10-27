"""The graph Aegra loads. Aegra supplies the Postgres checkpointer."""

import os

from home_keeper_agent.graph import build_agent
from home_keeper_agent.keeper import KeeperClient
from home_keeper_agent.model import chat_model
from home_keeper_renamed import reject_renamed_variables

# `aegra dev` loads this module directly, without serve.main's check.
reject_renamed_variables(os.environ)
agent = build_agent(chat_model(), KeeperClient.from_env())
