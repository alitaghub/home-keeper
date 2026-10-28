from pathlib import Path

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from home_keeper_settings import OptionalOrigin, Origin, Port, SettingsError, load, parse_origin


class Example(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXAMPLE_")

    port: Port = 8000
    origin: Origin = "http://localhost:3000"
    optional: OptionalOrigin = "http://controller:8080"
    path: Path = Path("/secrets/token")


@pytest.mark.parametrize(
    ("value", "origin"),
    [
        ("http://127.0.0.1:3000", "http://127.0.0.1:3000"),
        ("http://127.0.0.1:8123/", "http://127.0.0.1:8123"),
        ("https://Chat.Example.test", "https://chat.example.test"),
        ("http://[::1]:3000", "http://[::1]:3000"),
    ],
)
def test_an_origin_is_reduced_to_scheme_host_and_port(value: str, origin: str) -> None:
    assert parse_origin(value) == origin


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:3000/chat",
        "http://127.0.0.1:3000?next=1",
        "http://127.0.0.1:3000#top",
        "http://user:secret@127.0.0.1:3000",
        "http://127.0.0.1:99999",
        "127.0.0.1:3000",
        "javascript:alert(1)",
    ],
    ids=["path", "query", "fragment", "credentials", "bad-port", "no-scheme", "javascript"],
)
def test_anything_but_an_http_origin_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="must be an http"):
        parse_origin(value)


def test_unset_variables_keep_their_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("EXAMPLE_PORT", "EXAMPLE_ORIGIN", "EXAMPLE_OPTIONAL", "EXAMPLE_PATH"):
        monkeypatch.delenv(name, raising=False)

    settings = load(Example)

    assert settings.port == 8000
    assert settings.optional == "http://controller:8080"
    assert settings.path == Path("/secrets/token")


def test_variables_are_parsed_into_their_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_PORT", "9001")
    monkeypatch.setenv("EXAMPLE_ORIGIN", "http://ha.example.test:8123/")
    monkeypatch.setenv("EXAMPLE_PATH", "/run/secrets/token")

    settings = load(Example)

    assert settings.port == 9001
    assert settings.origin == "http://ha.example.test:8123"
    assert settings.path == Path("/run/secrets/token")


def test_an_empty_optional_origin_means_none_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPLE_OPTIONAL", "")

    assert load(Example).optional is None


def test_every_invalid_variable_is_named_without_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXAMPLE_PORT", "eighty")
    monkeypatch.setenv("EXAMPLE_ORIGIN", "http://user:origin-sentinel@host")

    with pytest.raises(SettingsError) as error:
        load(Example)

    assert error.value.variables == ("EXAMPLE_PORT", "EXAMPLE_ORIGIN")
    assert "EXAMPLE_PORT: Input should be a valid integer" in str(error.value)
    assert "origin-sentinel" not in str(error.value)
