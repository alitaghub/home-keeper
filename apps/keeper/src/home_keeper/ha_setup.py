"""One-shot setup for the bundled Home Assistant: onboarding without a browser, then a
long-lived owner token for the collector and one for a non-admin control user.
"""

import json
import logging
import os
import secrets
import sys
import time
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ValidationError
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from home_keeper.runtime import parse_origin

OWNER_USERNAME = "owner"
CONTROL_NAME = "Home Keeper control"
CONTROL_USERNAME = "home-keeper-control"
# Home Assistant's built-in groups for users without and with admin rights.
CONTROL_GROUP = "system-users"
ADMIN_GROUP = "system-admin"
READY_TIMEOUT_SECONDS = 300
TOKEN_LIFESPAN_DAYS = 3650
RESET_HINT = (
    "Reset the bundled Home Assistant with `docker compose down --volumes` "
    "(this also deletes Home Keeper history), or use compose.external.yaml with your own token."
)


class SetupError(Exception):
    pass


class WebSocket(Protocol):
    """The part of a websockets sync connection setup uses."""

    def send(self, message: str, /) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...


class OnboardingStep(BaseModel):
    step: str
    done: bool


class AuthCode(BaseModel):
    auth_code: str


class AccessToken(BaseModel):
    access_token: str


class WebSocketAuth(BaseModel):
    type: str


class WebSocketResult(BaseModel):
    success: bool
    result: str | None = None


class CommandResult(BaseModel):
    """A websocket command's reply, when only success matters."""

    id: int
    type: Literal["result"]
    success: bool


class User(BaseModel):
    id: str
    name: str | None = None
    username: str | None = None
    is_owner: bool = False
    system_generated: bool = False
    group_ids: list[str] = []


class CreatedUser(BaseModel):
    user: User


class UserList(CommandResult):
    result: list[User]


class UserCreated(CommandResult):
    result: CreatedUser


class LoginFlow(BaseModel):
    flow_id: str


class LoginFlowResult(BaseModel):
    type: str
    # The authorization code, once the flow creates an entry.
    result: str | None = None


class Login(BaseModel):
    username: str
    password: str


def write_private(path: Path, content: str) -> None:
    """Replace the file atomically, readable only by the container user."""
    staging = path.with_name(f".{path.name}.tmp")
    staging.unlink(missing_ok=True)
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
    staging.replace(path)


class Setup:
    def __init__(
        self,
        origin: str,
        token_file: Path,
        login_file: Path,
        transport: httpx.BaseTransport | None = None,
        control_token_file: Path | None = None,
    ) -> None:
        self.origin = origin.rstrip("/")
        # Home Assistant accepts any http(s) URL as an OAuth client ID.
        self.client_id = f"{self.origin}/"
        self.token_file = token_file
        self.login_file = login_file
        # None skips the control user, for callers that only need the collector's token.
        self.control_token_file = control_token_file
        self.http = httpx.Client(
            base_url=self.origin,
            timeout=10,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def run(self) -> None:
        if self.token_file.exists():
            if not self.token_accepted(self.token_file):
                raise SetupError(
                    "Home Assistant rejected the stored Home Keeper token. " + RESET_HINT
                )
            collector_token = self.token_file.read_text().strip()
            if self.control_token_file is None:
                logging.info("Home Assistant already set up; stored token is valid")
            elif not self.control_token_file.exists():
                self.provision_control(collector_token)
            elif self.token_accepted(self.control_token_file):
                logging.info("Home Assistant already set up; stored tokens are valid")
            else:
                # Control is optional: collection starts, and setup leaves the revocation alone.
                logging.warning(
                    "Home Assistant rejected the stored control token, so light control is off. "
                    "Delete the control token file to create a new one."
                )
            return
        steps = self.wait_for_onboarding_status()
        if steps is None or any(step.done for step in steps):
            raise SetupError(
                "Home Assistant is already onboarded but no Home Keeper token is stored. "
                + RESET_HINT
            )
        login = Login(username=OWNER_USERNAME, password=secrets.token_urlsafe(18))
        # Stored before the account exists, so a crash cannot leave an owner nobody can log in as.
        write_private(self.login_file, login.model_dump_json())
        collector_token = self.create_long_lived_token(self.onboard(login))
        write_private(self.token_file, collector_token)
        if self.control_token_file is not None:
            self.provision_control(collector_token)
        logging.info(
            "Home Assistant is ready. Show the login: "
            "docker compose run --rm --no-deps ha-setup login"
        )

    def wait_for_onboarding_status(self) -> list[OnboardingStep] | None:
        response = self.wait_for_ready_response("/api/onboarding")
        if response.status_code == HTTPStatus.NOT_FOUND:
            # Home Assistant removes this endpoint once onboarding is complete.
            return None
        response.raise_for_status()
        return [OnboardingStep.model_validate(step) for step in response.json()]

    def wait_for_ready_response(
        self, path: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        waiting = False
        while True:
            try:
                response = self.http.get(path, headers=headers)
                if not response.is_server_error:
                    return response
                failure = f"HTTP {response.status_code}"
            except httpx.HTTPError as error:
                failure = type(error).__name__
            if not waiting:
                logging.info("Waiting for Home Assistant to start (%s)", failure)
                waiting = True
            if time.monotonic() > deadline:
                raise SetupError(
                    f"Home Assistant did not start within five minutes; last failure: {failure}"
                )
            time.sleep(2)

    def token_accepted(self, token_file: Path) -> bool:
        token = token_file.read_text().strip()
        response = self.wait_for_ready_response(
            "/api/", headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            return False
        response.raise_for_status()
        return True

    def log_in(self, login: Login) -> str:
        """Run Home Assistant's login flow as a browser does; return a short-lived access token."""
        flow = LoginFlow.model_validate(
            self.post(
                "/auth/login_flow",
                json={
                    "client_id": self.client_id,
                    "handler": ["homeassistant", None],
                    "redirect_uri": f"{self.client_id}?auth_callback=1",
                },
            )
        )
        result = LoginFlowResult.model_validate(
            self.post(
                f"/auth/login_flow/{flow.flow_id}",
                json={"client_id": self.client_id} | login.model_dump(),
            )
        )
        if result.type != "create_entry" or not result.result:
            raise SetupError(f"Home Assistant refused the login for {login.username}")
        return AccessToken.model_validate(
            self.post(
                "/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": result.result,
                    "client_id": self.client_id,
                },
            )
        ).access_token

    def provision_control(self, owner_token: str) -> None:
        """Create the control user and its token. Control is optional, so a failure only turns
        it off: the collector waits for setup to succeed, and collection must still start."""
        logging.info("Creating the Home Keeper control user")
        try:
            self.create_control_token(owner_token)
        except ValidationError:
            # Validation messages quote their input, which can hold a token or password.
            logging.warning(
                "Light control is off: unexpected reply from Home Assistant. "
                "The next `up` tries again."
            )
        except (SetupError, httpx.HTTPError, WebSocketException, OSError) as error:
            logging.warning("Light control is off. The next `up` tries again. Cause: %s", error)

    def create_control_token(self, owner_token: str) -> None:
        """Create the non-admin control user with the owner's token; store the user's token."""
        if self.control_token_file is None:
            return
        # The password is only for minting the token below, so it is not kept.
        login = Login(username=CONTROL_USERNAME, password=secrets.token_urlsafe(18))
        with self.websocket(owner_token) as socket:
            commands = iter(range(1, 100))
            if not self.add_control_user(socket, commands, login):
                # The username is taken by a control user whose token is gone; replace it.
                self.remove_control_users(socket, commands)
                if not self.add_control_user(socket, commands, login):
                    raise SetupError(
                        "Home Assistant did not create the Home Keeper control user. "
                        f"Another account may use the username {CONTROL_USERNAME}."
                    )
        token = self.create_long_lived_token(self.log_in(login), "Home Keeper control")
        write_private(self.control_token_file, token)

    @staticmethod
    def add_control_user(socket: WebSocket, commands: Iterator[int], login: Login) -> bool:
        identity = next(commands)
        socket.send(
            json.dumps(
                {
                    "id": identity,
                    "type": "config/auth/create",
                    "name": CONTROL_NAME,
                    "group_ids": [CONTROL_GROUP],
                }
            )
        )
        reply = socket.recv()
        if not CommandResult.model_validate_json(reply).success:
            raise SetupError("Home Assistant did not create the Home Keeper control user")
        created = UserCreated.model_validate_json(reply)
        user_id = created.result.user.id
        socket.send(
            json.dumps(
                {
                    "id": next(commands),
                    "type": "config/auth_provider/homeassistant/create",
                    "user_id": user_id,
                }
                | login.model_dump()
            )
        )
        if CommandResult.model_validate_json(socket.recv()).success:
            return True
        # This run's account has no login; remove it rather than leave it behind.
        Setup.delete_user(socket, commands, user_id)
        return False

    @staticmethod
    def remove_control_users(socket: WebSocket, commands: Iterator[int]) -> None:
        """Delete the non-admin account that holds the control username.

        Only the username identifies it: display names are not unique.
        """
        socket.send(json.dumps({"id": next(commands), "type": "config/auth/list"}))
        reply = socket.recv()
        if not CommandResult.model_validate_json(reply).success:
            raise SetupError("Home Assistant did not list its users")
        for user in UserList.model_validate_json(reply).result:
            if (
                user.username == CONTROL_USERNAME
                and not user.is_owner
                and not user.system_generated
                and ADMIN_GROUP not in user.group_ids
            ):
                Setup.delete_user(socket, commands, user.id)

    @staticmethod
    def delete_user(socket: WebSocket, commands: Iterator[int], user_id: str) -> None:
        socket.send(
            json.dumps({"id": next(commands), "type": "config/auth/delete", "user_id": user_id})
        )
        if not CommandResult.model_validate_json(socket.recv()).success:
            raise SetupError("Home Assistant did not remove a stale control user")

    def onboard(self, login: Login) -> str:
        user = AuthCode.model_validate(
            self.post(
                "/api/onboarding/users",
                json={"client_id": self.client_id, "name": "Owner", "language": "en"}
                | login.model_dump(),
            )
        )
        token = AccessToken.model_validate(
            self.post(
                "/auth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": user.auth_code,
                    "client_id": self.client_id,
                },
            )
        ).access_token
        headers = {"Authorization": f"Bearer {token}"}
        # Analytics stays disabled: finishing the step does not opt in.
        self.post("/api/onboarding/core_config", headers=headers)
        self.post("/api/onboarding/analytics", headers=headers)
        self.post(
            "/api/onboarding/integration",
            headers=headers,
            json={"client_id": self.client_id, "redirect_uri": f"{self.client_id}?auth_callback=1"},
        )
        return token

    def post(
        self,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        response = self.http.post(path, json=json, data=data, headers=headers)
        if response.is_error:
            raise SetupError(f"Home Assistant refused {path} with status {response.status_code}")
        return response.json()

    @contextmanager
    def websocket(self, access_token: str) -> Generator[WebSocket]:
        scheme = "wss" if self.origin.startswith("https") else "ws"
        with connect(f"{scheme}://{self.origin.split('://', 1)[1]}/api/websocket") as socket:
            socket.recv()
            socket.send(json.dumps({"type": "auth", "access_token": access_token}))
            if WebSocketAuth.model_validate_json(socket.recv()).type != "auth_ok":
                raise SetupError("Home Assistant rejected the setup session")
            yield socket

    def create_long_lived_token(self, access_token: str, client_name: str = "Home Keeper") -> str:
        with self.websocket(access_token) as socket:
            socket.send(
                json.dumps(
                    {
                        "id": 1,
                        "type": "auth/long_lived_access_token",
                        "client_name": client_name,
                        "lifespan": TOKEN_LIFESPAN_DAYS,
                    }
                )
            )
            reply = WebSocketResult.model_validate_json(socket.recv())
        if not reply.success or not reply.result:
            raise SetupError(f"Home Assistant did not create the {client_name} token")
        return reply.result


def show_login(login_file: Path, public_url: str) -> None:
    try:
        login = Login.model_validate_json(login_file.read_text())
    except FileNotFoundError:
        raise SetupError("No login stored yet. Run `docker compose up --wait` first.") from None
    except ValidationError:
        # The error text would include the stored password.
        raise SetupError("The stored login is unreadable.") from None
    print(f"Home Assistant: {public_url or '(see HA_PORT)'}")
    print(f"Username: {login.username}")
    print(f"Password: {login.password}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token_file = Path(os.environ.get("HOME_KEEPER_HA_TOKEN_FILE", "/ha-token/token"))
    control_token_file = Path(
        os.environ.get("HOME_KEEPER_HA_CONTROL_TOKEN_FILE", "/ha-control-token/token")
    )
    login_file = Path(os.environ.get("HOME_KEEPER_HA_LOGIN_FILE", "/ha-owner/login.json"))
    try:
        if sys.argv[1:] == ["login"]:
            show_login(login_file, os.environ.get("HOME_KEEPER_HA_PUBLIC_URL", ""))
        elif sys.argv[1:] in ([], ["setup"]):
            origin = parse_origin("HOME_KEEPER_HA_URL", os.environ.get("HOME_KEEPER_HA_URL", ""))
            if origin is None:
                raise SetupError("Set HOME_KEEPER_HA_URL to the Home Assistant origin")
            Setup(origin, token_file, login_file, control_token_file=control_token_file).run()
        else:
            raise SetupError("Usage: ha_setup [setup|login]")
    except ValidationError:
        # Validation messages quote their input, which can hold a token or password.
        logging.error("Home Assistant setup failed: unexpected reply from Home Assistant")
        raise SystemExit(1) from None
    except (SetupError, httpx.HTTPError, WebSocketException, OSError, ValueError) as error:
        logging.error("Home Assistant setup failed: %s", error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
