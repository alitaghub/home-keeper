import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from support.home_assistant import FakeSocket

from home_observer.ha_setup import Login, Setup, SetupError, show_login

ORIGIN = "http://homeassistant:8123"


class FakeHomeAssistant:
    """Answers the onboarding and auth routes the way Home Assistant 2026.10.1 did in a spike."""

    def __init__(self, *, onboarded: bool = False, valid_token: str = "") -> None:
        self.onboarded = onboarded
        self.valid_token = valid_token
        self.posts: list[tuple[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/onboarding":
            steps = ("user", "core_config", "analytics", "integration")
            return httpx.Response(200, json=[{"step": s, "done": self.onboarded} for s in steps])
        if request.method == "GET" and path == "/api/":
            authorized = request.headers.get("Authorization") == f"Bearer {self.valid_token}"
            return httpx.Response(200 if authorized else 401, json={})
        body = json.loads(request.content) if request.content.startswith(b"{") else None
        self.posts.append((path, body))
        if path == "/api/onboarding/users":
            if self.onboarded:
                return httpx.Response(403, json={"message": "User step already done"})
            self.onboarded = True
            return httpx.Response(200, json={"auth_code": "code"})
        if path == "/auth/token":
            return httpx.Response(200, json={"access_token": "short-lived"})
        return httpx.Response(200, json={})


def token_socket(result: str | None = "long-lived") -> FakeSocket:
    socket = FakeSocket([])
    socket.messages.clear()
    socket.messages.extend(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": result is not None, "result": result},
        ]
    )
    return socket


def setup(tmp_path: Path, home: FakeHomeAssistant) -> Setup:
    return Setup(
        ORIGIN, tmp_path / "token", tmp_path / "login.json", transport=httpx.MockTransport(home)
    )


def install(monkeypatch: pytest.MonkeyPatch, socket: FakeSocket) -> None:
    def connect(*_args: object, **_kwargs: object) -> FakeSocket:
        return socket

    monkeypatch.setattr("home_observer.ha_setup.connect", connect)


def test_fresh_home_assistant_is_onboarded_and_its_token_stored_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = FakeHomeAssistant()
    socket = token_socket()
    install(monkeypatch, socket)

    setup(tmp_path, home).run()

    assert [path for path, _ in home.posts] == [
        "/api/onboarding/users",
        "/auth/token",
        "/api/onboarding/core_config",
        "/api/onboarding/analytics",
        "/api/onboarding/integration",
    ]
    login = Login.model_validate_json((tmp_path / "login.json").read_text())
    assert home.posts[0][1]["password"] == login.password
    # An empty analytics step finishes onboarding without opting in.
    assert home.posts[3] == ("/api/onboarding/analytics", None)
    assert login.username == "owner"
    assert (tmp_path / "token").read_text() == "long-lived"
    assert socket.sent[0] == {"type": "auth", "access_token": "short-lived"}
    assert socket.sent[1]["type"] == "auth/long_lived_access_token"
    for name in ("token", "login.json"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_valid_stored_token_leaves_home_assistant_untouched(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("kept")
    home = FakeHomeAssistant(onboarded=True, valid_token="kept")

    setup(tmp_path, home).run()

    assert home.posts == []
    assert (tmp_path / "token").read_text() == "kept"


def test_onboarded_home_assistant_without_a_token_fails_with_reset_guidance(
    tmp_path: Path,
) -> None:
    home = FakeHomeAssistant(onboarded=True)

    with pytest.raises(SetupError, match="docker compose down --volumes"):
        setup(tmp_path, home).run()

    assert home.posts == []
    assert not (tmp_path / "login.json").exists()


def test_rejected_stored_token_fails_instead_of_minting_a_new_one(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("revoked")
    home = FakeHomeAssistant(onboarded=True, valid_token="other")

    with pytest.raises(SetupError, match="rejected the stored Observer token"):
        setup(tmp_path, home).run()

    assert home.posts == []


def test_refused_long_lived_token_stores_nothing_for_the_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, token_socket(result=None))

    with pytest.raises(SetupError, match="did not create the Observer token"):
        setup(tmp_path, FakeHomeAssistant()).run()

    assert not (tmp_path / "token").exists()
    # The owner exists now, so its login must survive for the human.
    assert (tmp_path / "login.json").exists()


def test_login_is_shown_again_from_the_stored_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "login.json").write_text(Login(username="owner", password="pw").model_dump_json())

    show_login(tmp_path / "login.json", "http://127.0.0.1:8123")

    assert capsys.readouterr().out.splitlines() == [
        "Home Assistant: http://127.0.0.1:8123",
        "Username: owner",
        "Password: pw",
    ]
    with pytest.raises(SetupError, match="No login stored yet"):
        show_login(tmp_path / "missing.json", "")
