from pathlib import Path

import pytest

from home_keeper_agent import serve
from home_keeper_agent.serve import aegra_config


def test_aegra_config_allows_only_the_chat_origin_and_uses_an_absolute_graph_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    config = aegra_config("http://127.0.0.1:3000", Path("server.py"))

    assert config == {
        "graphs": {"agent": f"{tmp_path.resolve() / 'server.py'}:agent"},
        "http": {"cors": {"allow_origins": ["http://127.0.0.1:3000"]}},
    }


def test_a_chat_origin_with_a_path_stops_the_server_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_ORIGIN", "http://127.0.0.1:3000/chat")

    with pytest.raises(SystemExit, match="CHAT_ORIGIN"):
        serve.main()


def test_aegra_gets_its_database_password_from_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "password").write_text("db-password-sentinel\n")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(tmp_path / "password"))
    # Set first, so the test restores it after main overwrites it.
    monkeypatch.setenv("POSTGRES_PASSWORD", "")
    started: list[list[str]] = []

    def execvp(_file: str, command: list[str]) -> None:
        started.append(command)

    monkeypatch.setattr(serve.os, "execvp", execvp)

    serve.main()

    assert serve.os.environ["POSTGRES_PASSWORD"] == "db-password-sentinel"
    assert started[0][:2] == ["aegra", "serve"]
