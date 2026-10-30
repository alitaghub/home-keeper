"""The environment variables the agent server reads; the Telegram bot has its own."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from home_keeper_settings import OptionalOrigin


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(str_strip_whitespace=True)

    # A provider:model string for init_chat_model; empty means model.DEFAULT_MODEL.
    agent_model: str = ""
    # Required for azure: models only.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: SecretStr = SecretStr("")
    home_keeper_url: str = "http://api:8000"
    # The only browser origin Aegra allows (CORS); empty means serve.DEFAULT_CHAT_ORIGIN.
    chat_origin: OptionalOrigin = None
    # Aegra's Postgres password, which serve passes on as POSTGRES_PASSWORD. Aegra reads its
    # other POSTGRES_* variables itself. None leaves POSTGRES_PASSWORD as it is.
    database_password_file: Path | None = None
