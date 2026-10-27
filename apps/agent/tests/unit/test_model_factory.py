import pytest
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from home_keeper_agent.model import chat_model

MODEL_VARIABLES = ("AGENT_MODEL", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY")


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MODEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("agent_model", "deployment"),
    [(None, "gpt-6.1-sol"), ("azure:hallway-deployment", "hallway-deployment")],
)
def test_azure_model_calls_the_deployment_on_the_azure_v1_url(
    monkeypatch: pytest.MonkeyPatch, agent_model: str | None, deployment: str
) -> None:
    if agent_model is not None:
        monkeypatch.setenv("AGENT_MODEL", agent_model)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://home.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key-sentinel")

    model = chat_model()

    assert isinstance(model, ChatOpenAI)
    assert model.model_name == deployment
    assert model.openai_api_base == "https://home.openai.azure.com/openai/v1"
    assert isinstance(model.openai_api_key, SecretStr)
    assert model.openai_api_key.get_secret_value() == "azure-key-sentinel"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://home.cognitiveservices.azure.com/openai",
        "https://home.cognitiveservices.azure.com/openai/",
        "https://home.cognitiveservices.azure.com/openai/v1",
    ],
)
def test_azure_endpoint_that_already_names_the_openai_path_is_not_doubled(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key-sentinel")

    model = chat_model()

    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == "https://home.cognitiveservices.azure.com/openai/v1"


def test_other_provider_prefix_passes_through_without_azure_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_MODEL", "openai:gpt-4.1-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key-sentinel")

    model = chat_model()

    # init_chat_model strips the prefix and uses the provider's own endpoint.
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-4.1-mini"
    assert model.openai_api_base is None


@pytest.mark.parametrize("missing", ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"])
def test_missing_azure_setting_fails_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    settings = {
        "AZURE_OPENAI_ENDPOINT": "https://home.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "azure-key-sentinel",
    }
    for name, value in settings.items():
        if name != missing:
            monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=missing):
        chat_model()
