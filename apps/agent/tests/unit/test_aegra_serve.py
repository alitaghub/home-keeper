import json
from pathlib import Path

import pytest

from home_keeper_agent import serve
from home_keeper_agent.serve import aegra_config


def test_aegra_config_allows_no_origin_and_uses_an_absolute_graph_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    config = aegra_config(Path("server.py"))

    # Spelled out: without a cors section Aegra allows every origin ("*").
    assert config["http"] == {"cors": {"allow_origins": []}}
    assert config == {
        "graphs": {"agent": f"{tmp_path.resolve() / 'server.py'}:agent"},
        "http": {"cors": {"allow_origins": []}},
    }


def test_aegra_gets_its_database_password_and_a_config_that_allows_no_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "password").write_text("db-password-sentinel\n")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(tmp_path / "password"))
    # Set first, so the test restores it after main overwrites it.
    monkeypatch.setenv("POSTGRES_PASSWORD", "")
    monkeypatch.setattr(serve.tempfile, "tempdir", str(tmp_path))
    started: list[list[str]] = []

    # Recorded, never exec'd: a real exec would replace pytest with `aegra serve`.
    def execvp(_file: str, command: list[str]) -> None:
        started.append(command)

    monkeypatch.setattr(serve.os, "execvp", execvp)

    serve.main()

    assert serve.os.environ["POSTGRES_PASSWORD"] == "db-password-sentinel"
    [command] = started
    assert command[:2] == ["aegra", "serve"]
    config = Path(command[command.index("--config") + 1])
    assert config.parent == tmp_path
    assert json.loads(config.read_text())["http"] == {"cors": {"allow_origins": []}}
