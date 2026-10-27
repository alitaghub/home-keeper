from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from home_keeper import runtime
from home_keeper.runtime import local_authorities, parse_origin


def test_default_http_port_is_omitted_like_clients_omit_it() -> None:
    assert local_authorities(80, ("localhost", "api")) == ("localhost", "api")


def test_other_ports_are_part_of_the_authority() -> None:
    assert local_authorities(8000, ("localhost", "api")) == ("localhost:8000", "api:8000")


def test_configured_url_is_reduced_to_its_origin() -> None:
    assert parse_origin("URL", "http://127.0.0.1:8123/") == "http://127.0.0.1:8123"
    assert parse_origin("URL", "") is None


@pytest.mark.parametrize(
    "value",
    ["javascript:alert(1)", "http://user:pass@ha:8123", "http://ha:8123/api", "http://ha:99999"],
)
def test_configured_url_rejects_anything_but_an_http_origin(value: str) -> None:
    with pytest.raises(ValueError, match="URL must be an http"):
        parse_origin("URL", value)


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
        "HOME_KEEPER_DB": str(tmp_path / "home.sqlite"),
        "HOME_KEEPER_SOURCE_ID": "home_assistant",
        "HOME_KEEPER_HA_PUBLIC_URL": "http://ha.example.test:8123",
        "HOME_KEEPER_RETENTION_DAYS": "7",
        "HOME_KEEPER_RECONCILIATION_SECONDS": "3",
        "HOME_KEEPER_TIMEZONE": "Europe/Copenhagen",
        "HOME_KEEPER_BIND": "127.0.0.2",
    }.items():
        monkeypatch.setenv(name, value)

    runtime.main()

    assert received["db_path"] == tmp_path / "home.sqlite"
    assert received["source_id"] == "home_assistant"
    assert received["home_assistant_url"] == "http://ha.example.test:8123"
    assert received["retention_days"] == 7
    assert received["reconciliation_seconds"] == 3
    assert received["timezone"] == ZoneInfo("Europe/Copenhagen")
    assert received["host"] == "127.0.0.2"
    assert received["port"] == 9001
    assert "localhost:9001" in cast(tuple[str, ...], received["allowed_hosts"])
