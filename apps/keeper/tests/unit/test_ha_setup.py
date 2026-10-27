import json
import logging
import stat
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from home_keeper.ha_setup import Login, Setup, SetupError, show_login

ORIGIN = "http://homeassistant:8123"
ADMIN = "system-admin"
USERS = "system-users"
CONTROL_USER = "Home Keeper control"
OWNER_PASSWORD = "owner-password"

# Raw Home Assistant frames are untyped JSON.
type Frame = dict[str, Any]


@dataclass
class User:
    id: str
    name: str
    group_ids: list[str]
    is_owner: bool = False

    @property
    def is_admin(self) -> bool:
        return ADMIN in self.group_ids

    def frame(self, username: str | None = None) -> Frame:
        return {
            "id": self.id,
            "name": self.name,
            "username": username,
            "is_owner": self.is_owner,
            "is_admin": self.is_admin,
            "is_active": True,
            "system_generated": False,
            "group_ids": self.group_ids,
        }


class FakeHomeAssistant:
    """Answers onboarding, login flow, token and user administration the way Home Assistant
    2026.10.1 does: users, credentials and tokens are state, and admin commands need an admin."""

    def __init__(
        self,
        *,
        onboarded: bool = False,
        valid_token: str = "",
        owner_password: str = OWNER_PASSWORD,
        refuse_long_lived: bool = False,
    ) -> None:
        self.onboarded = onboarded
        self.refuse_long_lived = refuse_long_lived
        self.users: dict[str, User] = {}
        # Username to (password, user ID).
        self.credentials: dict[str, tuple[str, str]] = {}
        # Every bearer token Home Assistant accepts, to its user ID.
        self.tokens: dict[str, str] = {}
        self.long_lived: dict[str, str] = {}
        self.codes: dict[str, str] = {}
        self.flows: set[str] = set()
        self.posts: list[tuple[str, Any]] = []
        self.sessions: list[FakeSession] = []
        if onboarded:
            owner = self.add_user("Owner", [ADMIN], owner=True)
            self.credentials["owner"] = (owner_password, owner.id)
            if valid_token:
                self.tokens[valid_token] = self.long_lived[valid_token] = owner.id

    def add_user(self, name: str, group_ids: list[str], *, owner: bool = False) -> User:
        user = User(uuid4().hex, name, group_ids, owner)
        self.users[user.id] = user
        return user

    def code_for(self, user_id: str) -> str:
        code = uuid4().hex
        self.codes[code] = user_id
        return code

    def user_of(self, token: str) -> User:
        return self.users[self.long_lived[token]]

    def username_of(self, user_id: str) -> str | None:
        return next((name for name, (_, uid) in self.credentials.items() if uid == user_id), None)

    def add_control_user(self) -> User:
        """A control user with a login nobody knows any more, as a previous setup run leaves it."""
        user = self.add_user(CONTROL_USER, [USERS])
        self.credentials["home-keeper-control"] = (uuid4().hex, user.id)
        return user

    def delete_user(self, user_id: str) -> None:
        self.users.pop(user_id)
        self.credentials = {k: v for k, v in self.credentials.items() if v[1] != user_id}
        for table in (self.tokens, self.long_lived):
            for token in [t for t, uid in table.items() if uid == user_id]:
                del table[token]

    def owner_logins(self) -> list[Any]:
        """Login-flow steps that sent the owner's username."""
        steps: list[Any] = []
        for path, body in self.posts:
            fields: dict[str, Any] = body or {}
            if path.startswith("/auth/login_flow/") and fields.get("username") == "owner":
                steps.append(body)
        return steps

    def __call__(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 -- one per route
        path = request.url.path
        if request.method == "GET" and path == "/api/onboarding":
            steps = ("user", "core_config", "analytics", "integration")
            return httpx.Response(200, json=[{"step": s, "done": self.onboarded} for s in steps])
        if request.method == "GET" and path == "/api/":
            bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
            return httpx.Response(200 if bearer in self.tokens else 401, json={})
        body = json.loads(request.content) if request.content.startswith(b"{") else None
        self.posts.append((path, body))
        if path == "/api/onboarding/users":
            if self.onboarded or body is None:
                return httpx.Response(403, json={"message": "User step already done"})
            self.onboarded = True
            owner = self.add_user(body["name"], [ADMIN], owner=True)
            self.credentials[body["username"]] = (body["password"], owner.id)
            return httpx.Response(200, json={"auth_code": self.code_for(owner.id)})
        if path == "/auth/token":
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            user_id = self.codes.pop(form.get("code", ""), None)
            if form.get("grant_type") != "authorization_code" or user_id is None:
                return httpx.Response(400, json={"error": "invalid_request"})
            token = f"access-{uuid4().hex}"
            self.tokens[token] = user_id
            return httpx.Response(
                200, json={"access_token": token, "token_type": "Bearer", "expires_in": 1800}
            )
        if path == "/auth/login_flow":
            flow = uuid4().hex
            self.flows.add(flow)
            return httpx.Response(
                200, json={"type": "form", "flow_id": flow, "step_id": "init", "errors": {}}
            )
        if path.startswith("/auth/login_flow/") and path.removeprefix("/auth/login_flow/") in (
            self.flows
        ):
            flow = path.removeprefix("/auth/login_flow/")
            fields: dict[str, Any] = body or {}
            stored = self.credentials.get(fields.get("username", ""))
            if stored is None or stored[0] != fields.get("password"):
                return httpx.Response(
                    200,
                    json={"type": "form", "flow_id": flow, "errors": {"base": "invalid_auth"}},
                )
            self.flows.discard(flow)
            return httpx.Response(
                200,
                json={"type": "create_entry", "flow_id": flow, "result": self.code_for(stored[1])},
            )
        if path.startswith("/api/onboarding/"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "Not found"})

    def answer(self, session: "FakeSession", frame: Frame) -> Frame:  # noqa: PLR0911 -- one per command
        if frame.get("type") == "auth":
            user_id = self.tokens.get(frame.get("access_token", ""))
            if user_id is None:
                return {"type": "auth_invalid", "message": "Invalid access token or password"}
            session.user = self.users[user_id]
            return {"type": "auth_ok", "ha_version": "2026.10.1"}
        user = session.user
        assert user is not None, "command sent before authentication"
        kind = frame.get("type")

        def result(value: object) -> Frame:
            return {"id": frame.get("id"), "type": "result", "success": True, "result": value}

        def error(code: str) -> Frame:
            return {
                "id": frame.get("id"),
                "type": "result",
                "success": False,
                "error": {"code": code, "message": code},
            }

        if kind == "auth/long_lived_access_token":
            if self.refuse_long_lived:
                return error("unknown_error")
            token = f"long-lived-{uuid4().hex}"
            self.tokens[token] = self.long_lived[token] = user.id
            return result(token)
        if kind == "auth/current_user":
            return result(user.frame())
        if str(kind).startswith(("config/auth/", "config/auth_provider/")):
            if not user.is_admin:
                return error("unauthorized")
            if kind == "config/auth/list":
                return result(
                    [known.frame(self.username_of(known.id)) for known in self.users.values()]
                )
            if kind == "config/auth/create":
                created = self.add_user(frame["name"], list(frame.get("group_ids", [])))
                return result({"user": created.frame()})
            if kind == "config/auth/delete":
                target = self.users.get(frame.get("user_id", ""))
                # Home Assistant refuses to delete the owner or the caller.
                if target is None or target.is_owner or target.id == user.id:
                    return error("home_assistant_error")
                self.delete_user(target.id)
                return result(None)
            if kind == "config/auth_provider/homeassistant/create":
                if frame["user_id"] not in self.users or frame["username"] in self.credentials:
                    return error("home_assistant_error")
                self.credentials[frame["username"]] = (frame["password"], frame["user_id"])
                return result(None)
            if kind == "config/auth_provider/homeassistant/admin_change_password":
                username = self.username_of(frame.get("user_id", ""))
                if username is None:
                    return error("credentials_not_found")
                self.credentials[username] = (frame["password"], frame["user_id"])
                return result(None)
        return error("unknown_command")


@dataclass
class FakeSession:
    """One websocket connection to the fake Home Assistant."""

    home: FakeHomeAssistant
    url: str
    user: User | None = None
    sent: list[Frame] = field(default_factory=list[Frame])
    outbox: deque[Frame] = field(default_factory=lambda: deque([{"type": "auth_required"}]))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def recv(self, timeout: float | None = None) -> str:
        assert self.outbox, "setup waited for a frame Home Assistant never sends"
        return json.dumps(self.outbox.popleft())

    def send(self, value: str) -> None:
        frame: Frame = json.loads(value)
        self.sent.append(frame)
        self.outbox.append(self.home.answer(self, frame))

    def close(self) -> None:
        pass


def setup(tmp_path: Path, home: FakeHomeAssistant) -> Setup:
    return Setup(
        ORIGIN,
        tmp_path / "token",
        tmp_path / "login.json",
        control_token_file=tmp_path / "control-token",
        transport=httpx.MockTransport(home),
    )


def install(monkeypatch: pytest.MonkeyPatch, home: FakeHomeAssistant) -> None:
    def connect(url: str, *_args: object, **_kwargs: object) -> FakeSession:
        session = FakeSession(home, url)
        home.sessions.append(session)
        return session

    monkeypatch.setattr("home_keeper.ha_setup.connect", connect)


def forbid_websocket(monkeypatch: pytest.MonkeyPatch) -> None:
    def connect(*_args: object, **_kwargs: object) -> FakeSession:
        raise AssertionError("setup opened a Home Assistant websocket")

    monkeypatch.setattr("home_keeper.ha_setup.connect", connect)


def assert_control_token(home: FakeHomeAssistant, tmp_path: Path) -> User:
    """The control token is private, distinct, and belongs to a non-admin control user."""
    path = tmp_path / "control-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    token = path.read_text()
    assert token != (tmp_path / "token").read_text()
    assert token in home.long_lived, "control token is not a long-lived token"
    user = home.user_of(token)
    assert user.name == CONTROL_USER
    assert not user.is_admin
    assert not user.is_owner
    # Home Assistant gives a user with no group no permissions at all.
    assert USERS in user.group_ids
    assert [known.name for known in home.users.values()].count(CONTROL_USER) == 1
    return user


def test_fresh_home_assistant_is_onboarded_and_both_tokens_stored_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    home = FakeHomeAssistant()
    install(monkeypatch, home)

    setup(tmp_path, home).run()

    assert [path for path, _ in home.posts][:5] == [
        "/api/onboarding/users",
        "/auth/token",
        "/api/onboarding/core_config",
        "/api/onboarding/analytics",
        "/api/onboarding/integration",
    ]
    login = Login.model_validate_json((tmp_path / "login.json").read_text())
    assert home.posts[0][1]["password"] == login.password
    assert login.password not in caplog.text
    # An empty analytics step finishes onboarding without opting in.
    assert home.posts[3] == ("/api/onboarding/analytics", None)
    assert login.username == "owner"
    collector = (tmp_path / "token").read_text()
    assert home.user_of(collector).is_owner
    for name in ("token", "login.json"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    control = assert_control_token(home, tmp_path)
    assert len(home.users) == 2
    control_passwords = [
        password for password, user_id in home.credentials.values() if user_id == control.id
    ]
    assert control_passwords, "the control user has no login to mint its token with"
    for secret in (collector, (tmp_path / "control-token").read_text(), *control_passwords):
        assert secret not in caplog.text


def test_upgraded_install_creates_the_control_token_with_the_collector_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    home = FakeHomeAssistant(onboarded=True, valid_token="kept", owner_password="stored-pw")
    install(monkeypatch, home)
    (tmp_path / "token").write_text("kept")
    stored_login = Login(username="owner", password="stored-pw").model_dump_json()
    (tmp_path / "login.json").write_text(stored_login)

    setup(tmp_path, home).run()

    assert "/api/onboarding/users" not in [path for path, _ in home.posts]
    assert (tmp_path / "token").read_text() == "kept"
    assert (tmp_path / "login.json").read_text() == stored_login
    assert_control_token(home, tmp_path)
    # The owner's long-lived collector token does the admin work; the password is not needed.
    assert home.owner_logins() == []
    assert "stored-pw" not in caplog.text


def test_upgrade_after_the_owner_changed_the_password_still_creates_the_control_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = FakeHomeAssistant(onboarded=True, valid_token="kept", owner_password="changed-pw")
    install(monkeypatch, home)
    (tmp_path / "token").write_text("kept")
    stale_login = Login(username="owner", password="stale-stored-pw").model_dump_json()
    (tmp_path / "login.json").write_text(stale_login)

    setup(tmp_path, home).run()

    assert_control_token(home, tmp_path)
    assert (tmp_path / "token").read_text() == "kept"
    assert (tmp_path / "login.json").read_text() == stale_login


def test_upgrade_without_a_stored_owner_login_still_creates_the_control_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = FakeHomeAssistant(onboarded=True, valid_token="kept")
    install(monkeypatch, home)
    (tmp_path / "token").write_text("kept")

    setup(tmp_path, home).run()

    assert_control_token(home, tmp_path)
    assert (tmp_path / "token").read_text() == "kept"
    assert not (tmp_path / "login.json").exists()


def test_revoked_control_token_leaves_control_off_and_collection_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    home = FakeHomeAssistant(onboarded=True, valid_token="kept")
    # The user deleted the control user's token in Home Assistant on purpose.
    control = home.add_control_user()
    install(monkeypatch, home)
    (tmp_path / "token").write_text("kept")
    (tmp_path / "control-token").write_text("revoked-control")
    users = {uid: user.frame(home.username_of(uid)) for uid, user in home.users.items()}
    credentials = dict(home.credentials)

    # Exiting normally lets Compose start the collector.
    setup(tmp_path, home).run()

    assert (tmp_path / "token").read_text() == "kept"
    assert (tmp_path / "control-token").read_text() == "revoked-control"
    # Revocation is respected: nobody recreates the control user or its login.
    assert {uid: user.frame(home.username_of(uid)) for uid, user in home.users.items()} == users
    assert home.credentials == credentials
    assert control.id in home.users
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "control" in record.getMessage().lower()
    ]
    assert warnings, "setup did not report that light control is off"
    assert "revoked-control" not in caplog.text


def test_deleting_the_control_token_file_provisions_a_fresh_usable_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = FakeHomeAssistant(onboarded=True, valid_token="kept")
    old = home.add_control_user()
    home.tokens["old-control"] = home.long_lived["old-control"] = old.id
    install(monkeypatch, home)
    (tmp_path / "token").write_text("kept")

    setup(tmp_path, home).run()

    assert_control_token(home, tmp_path)
    assert (tmp_path / "control-token").read_text() != "old-control"
    assert (tmp_path / "token").read_text() == "kept"


def test_rerun_after_a_half_made_control_user_leaves_one_usable_one_and_other_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = FakeHomeAssistant(onboarded=True, valid_token="kept")
    # A previous run created the control user and its login, then stopped before the token.
    home.add_control_user()
    family = home.add_user("Family", [ADMIN])
    home.credentials["family"] = ("family-pw", family.id)
    guest = home.add_user("Guest", [USERS])
    home.credentials["guest"] = ("guest-pw", guest.id)
    install(monkeypatch, home)
    (tmp_path / "token").write_text("kept")

    setup(tmp_path, home).run()

    assert_control_token(home, tmp_path)
    assert {family.id, guest.id} <= set(home.users)
    assert home.credentials["family"] == ("family-pw", family.id)
    assert home.credentials["guest"] == ("guest-pw", guest.id)
    assert sum(user.is_owner for user in home.users.values()) == 1


def test_both_stored_tokens_restart_without_requesting_onboarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "token").write_text("kept")
    (tmp_path / "control-token").write_text("kept-control")
    forbid_websocket(monkeypatch)
    paths: list[str] = []

    def home(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.url.path == "/api/"
        assert request.headers["Authorization"] in ("Bearer kept", "Bearer kept-control")
        return httpx.Response(200, json={})

    Setup(
        ORIGIN,
        tmp_path / "token",
        tmp_path / "login.json",
        control_token_file=tmp_path / "control-token",
        transport=httpx.MockTransport(home),
    ).run()

    assert paths
    assert set(paths) == {"/api/"}
    assert (tmp_path / "token").read_text() == "kept"
    assert (tmp_path / "control-token").read_text() == "kept-control"
    assert not (tmp_path / "login.json").exists()


def test_stored_token_waits_for_home_assistant_to_finish_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    (tmp_path / "token").write_text("kept")
    (tmp_path / "control-token").write_text("kept-control")
    forbid_websocket(monkeypatch)
    calls: list[str] = []
    sleeps: list[float] = []

    def home(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path == "/api/"
        if len(calls) == 1:
            raise httpx.ConnectError("Starting", request=request)
        return httpx.Response(503 if len(calls) == 2 else 200, json={})

    monkeypatch.setattr("home_keeper.ha_setup.time.sleep", sleeps.append)
    Setup(
        ORIGIN,
        tmp_path / "token",
        tmp_path / "login.json",
        control_token_file=tmp_path / "control-token",
        transport=httpx.MockTransport(home),
    ).run()

    assert calls[:3] == ["/api/"] * 3
    assert len(sleeps) == 2
    assert caplog.text.count("Waiting for Home Assistant") == 1
    assert "ConnectError" in caplog.text


@pytest.mark.parametrize("failure", ["connection", "server"])
def test_unavailable_home_assistant_stops_setup_after_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    (tmp_path / "token").write_text("kept")
    (tmp_path / "control-token").write_text("kept-control")
    forbid_websocket(monkeypatch)
    clock = iter([0, 0, 301])
    sleeps: list[float] = []
    paths: list[str] = []

    def home(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/api/"
        if failure == "connection":
            raise httpx.ConnectError("Starting", request=request)
        return httpx.Response(503)

    monkeypatch.setattr("home_keeper.ha_setup.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("home_keeper.ha_setup.time.sleep", sleeps.append)
    reason = "ConnectError" if failure == "connection" else "HTTP 503"
    with pytest.raises(SetupError, match=f"did not start within five minutes.*{reason}"):
        Setup(
            ORIGIN,
            tmp_path / "token",
            tmp_path / "login.json",
            control_token_file=tmp_path / "control-token",
            transport=httpx.MockTransport(home),
        ).run()

    assert paths == ["/api/", "/api/"]
    assert sleeps == [2]
    assert (tmp_path / "token").read_text() == "kept"


def test_onboarded_home_assistant_without_a_token_fails_with_reset_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = FakeHomeAssistant(onboarded=True)
    install(monkeypatch, home)

    with pytest.raises(SetupError, match="docker compose down --volumes"):
        setup(tmp_path, home).run()

    assert home.posts == []
    assert not (tmp_path / "login.json").exists()
    assert not (tmp_path / "control-token").exists()


def test_rejected_stored_token_fails_instead_of_minting_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "token").write_text("revoked")
    (tmp_path / "control-token").write_text("kept-control")
    home = FakeHomeAssistant(onboarded=True, valid_token="other")
    control = home.add_user(CONTROL_USER, [USERS])
    home.tokens["kept-control"] = home.long_lived["kept-control"] = control.id
    install(monkeypatch, home)

    with pytest.raises(SetupError, match="rejected the stored Home Keeper token"):
        setup(tmp_path, home).run()

    assert home.posts == []
    assert home.sessions == []


def test_refused_long_lived_token_stores_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = FakeHomeAssistant(refuse_long_lived=True)
    install(monkeypatch, home)

    with pytest.raises(SetupError):
        setup(tmp_path, home).run()

    assert not (tmp_path / "token").exists()
    assert not (tmp_path / "control-token").exists()
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


@pytest.mark.parametrize(
    ("name", "groups"),
    [
        pytest.param("Family account", [ADMIN], id="other-name"),
        # Display names are not unique in Home Assistant: a person may share the control name.
        pytest.param(CONTROL_USER, [ADMIN], id="same-name-admin"),
        pytest.param(CONTROL_USER, [USERS], id="same-name-user"),
    ],
)
def test_reprovisioning_keeps_any_user_that_is_not_the_control_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, groups: list[str]
) -> None:
    home = FakeHomeAssistant(onboarded=True, valid_token="kept")
    install(monkeypatch, home)
    (tmp_path / "token").write_text("kept")
    setup(tmp_path, home).run()
    old_token = (tmp_path / "control-token").read_text()
    other = home.add_user(name, groups)
    home.credentials["family-admin"] = ("family-password", other.id)
    home.tokens["family-token"] = home.long_lived["family-token"] = other.id

    # Deleting the token file is the documented way to get a fresh control token.
    (tmp_path / "control-token").unlink()
    setup(tmp_path, home).run()

    new_token = (tmp_path / "control-token").read_text()
    assert new_token != old_token
    assert home.user_of(new_token).id != other.id
    assert not home.user_of(new_token).is_admin
    assert home.username_of(home.user_of(new_token).id) == "home-keeper-control"
    assert other.id in home.users, "setup deleted a user that is not the control account"
    assert home.credentials["family-admin"] == ("family-password", other.id)
    assert home.tokens["family-token"] == other.id


def test_recovery_never_deletes_an_admin_holding_the_control_username(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = FakeHomeAssistant(onboarded=True, valid_token="kept")
    admin = home.add_user("Someone", [ADMIN])
    home.credentials["home-keeper-control"] = ("their-password", admin.id)
    install(monkeypatch, home)
    (tmp_path / "token").write_text("kept")

    # The username is taken, so control stays off; setup must not remove the admin.
    setup(tmp_path, home).run()

    assert admin.id in home.users
    assert home.credentials["home-keeper-control"] == ("their-password", admin.id)
    if (tmp_path / "control-token").exists():
        assert not home.user_of((tmp_path / "control-token").read_text()).is_admin


@pytest.mark.parametrize("failure", ["control username taken by an admin", "token refused"])
def test_failed_control_provisioning_on_upgrade_still_lets_collection_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    # Control is optional, and the collector waits for setup to succeed.
    home = FakeHomeAssistant(
        onboarded=True, valid_token="kept", refuse_long_lived=failure == "token refused"
    )
    if failure == "control username taken by an admin":
        admin = home.add_user("Someone", [ADMIN])
        home.credentials["home-keeper-control"] = ("their-password", admin.id)
    install(monkeypatch, home)
    (tmp_path / "token").write_text("kept")

    with caplog.at_level(logging.WARNING):
        setup(tmp_path, home).run()

    assert (tmp_path / "token").read_text() == "kept"
    assert not (tmp_path / "control-token").exists()
    assert "Light control is off" in caplog.text
