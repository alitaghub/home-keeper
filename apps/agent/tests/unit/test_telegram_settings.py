import pytest
from pydantic import SecretStr

from home_observer_agent.telegram import Settings


@pytest.mark.parametrize("allowed", ["", "alice", "1,", "0", "-1", "1,2.5"])
def test_invalid_allowlist_fails_with_a_secret_free_diagnostic(
    monkeypatch: pytest.MonkeyPatch, allowed: str
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:private-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", allowed)
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS") as caught:
        Settings.from_env()
    assert "private-secret" not in str(caught.value)


def test_settings_parse_family_ids_without_exposing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:private-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "11, 22,11")
    settings = Settings.from_env()
    assert settings.allowed_user_ids == frozenset({11, 22})
    assert settings.bot_token == SecretStr("123:private-secret")
    assert "private-secret" not in repr(settings)


def test_missing_token_fails_with_a_fixed_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "11")
    with pytest.raises(ValueError, match="Set TELEGRAM_BOT_TOKEN"):
        Settings.from_env()


@pytest.mark.parametrize("token", ["", "   "])
def test_empty_or_whitespace_token_is_rejected(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "11")
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()


def test_default_addresses_and_state_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:private-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "11")
    for name in ("TELEGRAM_AGENT_URL", "TELEGRAM_STATE_PATH", "TELEGRAM_API_URL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.agent_url == "http://agent:2026"
    assert settings.state_path == "/state/telegram.sqlite3"
    assert settings.api_url == "https://api.telegram.org"
