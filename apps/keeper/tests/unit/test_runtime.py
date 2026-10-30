from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from home_keeper import runtime
from home_keeper.runtime import local_authorities


def test_default_http_port_is_omitted_like_clients_omit_it() -> None:
    assert local_authorities(80, ("localhost", "api")) == ("localhost", "api")


def test_other_ports_are_part_of_the_authority() -> None:
    assert local_authorities(8000, ("localhost", "api")) == ("localhost:8000", "api:8000")


def test_settings_reach_the_app_and_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    received: dict[str, object] = {}

    def fake_create_app(**settings: object) -> str:
        received.update(settings)
        return "app"

    def fake_run(app: object, **options: object) -> None:
        received.update(app=app, **options)

    monkeypatch.setattr(runtime, "create_app", fake_create_app)
    monkeypatch.setattr(runtime.uvicorn, "run", fake_run)
    for name, value in {
        "HOME_KEEPER_TOKEN_FILE": str(tmp_path / "token"),
        "HOME_KEEPER_PORT": "9001",
        "HOME_KEEPER_DATABASE_URL": "postgresql://keeper@db.example.test/home",
        "HOME_KEEPER_DATABASE_PASSWORD_FILE": str(tmp_path / "db-password"),
        "HOME_KEEPER_SOURCE_ID": "home_assistant",
        "HOME_KEEPER_HA_PUBLIC_URL": "http://ha.example.test:8123",
        "HOME_KEEPER_RETENTION_DAYS": "7",
        "HOME_KEEPER_TIMEZONE": "Europe/Copenhagen",
        "HOME_KEEPER_BIND": "127.0.0.2",
    }.items():
        monkeypatch.setenv(name, value)

    (tmp_path / "db-password").write_text("db-password-sentinel\n")

    runtime.main()

    assert received["database"] == "postgresql://keeper@db.example.test/home"
    assert received["database_password"] == "db-password-sentinel"
    assert received["source_id"] == "home_assistant"
    assert received["home_assistant_url"] == "http://ha.example.test:8123"
    assert received["retention_days"] == 7
    assert received["timezone"] == ZoneInfo("Europe/Copenhagen")
    assert received["host"] == "127.0.0.2"
    assert received["port"] == 9001
    assert "localhost:9001" in cast(tuple[str, ...], received["allowed_hosts"])


def test_invalid_settings_stop_the_api_naming_each_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME_KEEPER_RETENTION_DAYS", "a month")
    monkeypatch.setenv("HOME_KEEPER_TIMEZONE", "Mars/Olympus_Mons")
    monkeypatch.setenv("HOME_KEEPER_HA_PUBLIC_URL", "http://owner:url-sentinel@ha:8123")

    with pytest.raises(SystemExit) as error:
        runtime.main()

    message = str(error.value.code)
    assert "HOME_KEEPER_RETENTION_DAYS" in message
    assert "HOME_KEEPER_TIMEZONE" in message
    assert "HOME_KEEPER_HA_PUBLIC_URL" in message
    assert "url-sentinel" not in message


def test_an_empty_controller_url_turns_control_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received: dict[str, object] = {}

    def fake_create_app(**settings: object) -> str:
        received.update(settings)
        return "app"

    monkeypatch.setattr(runtime, "create_app", fake_create_app)

    def fake_run(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr(runtime.uvicorn, "run", fake_run)
    monkeypatch.setenv("HOME_KEEPER_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("HOME_KEEPER_CONTROLLER_URL", "")
    monkeypatch.setenv("HOME_KEEPER_DATABASE_PASSWORD_FILE", str(tmp_path / "db-password"))
    (tmp_path / "db-password").write_text("db-password")

    runtime.main()

    assert received["controller"] is None
