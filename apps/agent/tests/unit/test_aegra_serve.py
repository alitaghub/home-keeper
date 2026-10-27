from pathlib import Path

import pytest

from home_keeper_agent.serve import aegra_config, parse_origin


@pytest.mark.parametrize(
    ("value", "origin"),
    [
        ("http://127.0.0.1:3000", "http://127.0.0.1:3000"),
        ("http://127.0.0.1:3000/", "http://127.0.0.1:3000"),
        ("https://chat.example.test", "https://chat.example.test"),
    ],
)
def test_chat_origin_is_accepted_as_a_bare_origin(value: str, origin: str) -> None:
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
    ],
    ids=["path", "query", "fragment", "credentials", "bad-port", "no-scheme"],
)
def test_chat_origin_with_more_than_an_origin_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="CHAT_ORIGIN"):
        parse_origin(value)


def test_aegra_config_allows_only_the_chat_origin_and_uses_an_absolute_graph_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    config = aegra_config("http://127.0.0.1:3000", Path("server.py"))

    assert config == {
        "graphs": {"agent": f"{tmp_path.resolve() / 'server.py'}:agent"},
        "http": {"cors": {"allow_origins": ["http://127.0.0.1:3000"]}},
    }
