import pytest
from pydantic import SecretStr

from home_keeper_agent.telegram import Settings


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


def test_missing_allowlist_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USER_IDS"):
        Settings.from_env()


def test_operator_can_override_addresses_and_state_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "11")
    monkeypatch.setenv("TELEGRAM_AGENT_URL", "http://alternate-agent:2026")
    monkeypatch.setenv("TELEGRAM_API_URL", "http://telegram.test:8000")
    monkeypatch.setenv("TELEGRAM_STATE_PATH", "/custom/bot.sqlite3")
    settings = Settings.from_env()
    assert settings.agent_url == "http://alternate-agent:2026"
    assert settings.api_url == "http://telegram.test:8000"
    assert settings.state_path == "/custom/bot.sqlite3"


def control_settings(
    monkeypatch: pytest.MonkeyPatch, allowed: str, control: str | None
) -> Settings:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:private-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", allowed)
    if control is None:
        monkeypatch.delenv("TELEGRAM_CONTROL_USER_IDS", raising=False)
    else:
        monkeypatch.setenv("TELEGRAM_CONTROL_USER_IDS", control)
    return Settings.from_env()


@pytest.mark.parametrize("control", [None, "", "  "])
def test_unset_or_empty_control_list_lets_nobody_control(
    monkeypatch: pytest.MonkeyPatch, control: str | None
) -> None:
    assert control_settings(monkeypatch, "11,22", control).control_user_ids == frozenset()


def test_control_ids_that_are_allowed_ids_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = control_settings(monkeypatch, "11,22,33", "22, 11,22")
    assert settings.control_user_ids == frozenset({11, 22})


def test_control_id_outside_the_allowed_ids_fails_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="TELEGRAM_CONTROL_USER_IDS") as caught:
        control_settings(monkeypatch, "11,22", "11,33")
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize("control", ["0", "-1", "alice", "11,", "1,2.5"])
def test_non_positive_or_malformed_control_ids_fail_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    with pytest.raises(ValueError, match="TELEGRAM_CONTROL_USER_IDS"):
        control_settings(monkeypatch, "11,22", control)
