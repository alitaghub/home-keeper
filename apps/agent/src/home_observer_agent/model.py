"""The only module that decides which chat model the agent uses."""

import os

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

DEFAULT_MODEL = "azure:gpt-6.1-sol"


def _required(variable: str) -> str:
    value = os.environ.get(variable, "").strip()
    if not value:
        raise ValueError(f"{variable} is required for azure: models")
    return value


def chat_model() -> BaseChatModel:
    """Build the model named by AGENT_MODEL, an init_chat_model-style provider:model string."""
    spec = os.environ.get("AGENT_MODEL", "").strip() or DEFAULT_MODEL
    provider, _, deployment = spec.partition(":")
    if provider != "azure":
        return init_chat_model(spec)
    if not deployment:
        raise ValueError("AGENT_MODEL needs a deployment name after azure:")
    endpoint = _required("AZURE_OPENAI_ENDPOINT")
    api_key = _required("AZURE_OPENAI_API_KEY")
    # Azure's v1 endpoint accepts the standard OpenAI client. Endpoints are often given with
    # the /openai or /openai/v1 path already on them.
    resource = endpoint.rstrip("/").removesuffix("/v1").removesuffix("/openai")
    return ChatOpenAI(
        model=deployment,
        base_url=f"{resource}/openai/v1",
        api_key=SecretStr(api_key),
    )
