"""The only module that decides which chat model the agent uses."""

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from home_keeper_agent.settings import AgentSettings

DEFAULT_MODEL = "azure:gpt-6.1-sol"


def _required(variable: str, value: str) -> str:
    if not value:
        raise ValueError(f"{variable} is required for azure: models")
    return value


def chat_model(settings: AgentSettings) -> BaseChatModel:
    """Build the model named by AGENT_MODEL, an init_chat_model-style provider:model string."""
    spec = settings.agent_model or DEFAULT_MODEL
    provider, _, deployment = spec.partition(":")
    if provider != "azure":
        return init_chat_model(spec)
    if not deployment:
        raise ValueError("AGENT_MODEL needs a deployment name after azure:")
    endpoint = _required("AZURE_OPENAI_ENDPOINT", settings.azure_openai_endpoint)
    api_key = _required(
        "AZURE_OPENAI_API_KEY", settings.azure_openai_api_key.get_secret_value().strip()
    )
    # Azure's v1 endpoint accepts the standard OpenAI client. Endpoints are often given with
    # the /openai or /openai/v1 path already on them.
    resource = endpoint.rstrip("/").removesuffix("/v1").removesuffix("/openai")
    return ChatOpenAI(
        model=deployment,
        base_url=f"{resource}/openai/v1",
        api_key=SecretStr(api_key),
    )
