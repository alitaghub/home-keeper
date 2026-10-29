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
