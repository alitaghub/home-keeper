"""One-shot setup for the bundled Home Assistant.

Completes Home Assistant onboarding without a browser, then stores a long-lived token
for the live collector. Re-running with a valid stored token changes nothing.
"""

import json
import logging
import os
import secrets
import sys
import time
from http import HTTPStatus
from pathlib import Path

import httpx
from pydantic import BaseModel
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from home_keeper.runtime import parse_origin

OWNER_USERNAME = "owner"
READY_TIMEOUT_SECONDS = 300
TOKEN_LIFESPAN_DAYS = 3650
RESET_HINT = (
    "Reset the bundled Home Assistant with `docker compose down --volumes` "
    "(this also deletes Home Keeper history), or use compose.external.yaml with your own token."
)


class SetupError(Exception):
    pass


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
    ) -> None:
        self.origin = origin.rstrip("/")
        # Home Assistant accepts any http(s) URL as an OAuth client ID.
        self.client_id = f"{self.origin}/"
        self.token_file = token_file
        self.login_file = login_file
        self.http = httpx.Client(
            base_url=self.origin,
            timeout=10,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def run(self) -> None:
        if self.token_file.exists():
            self.check_stored_token()
            logging.info("Home Assistant already set up; stored token is valid")
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
        access_token = self.onboard(login)
        write_private(self.token_file, self.create_long_lived_token(access_token))
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

    def check_stored_token(self) -> None:
        token = self.token_file.read_text().strip()
        response = self.wait_for_ready_response(
            "/api/", headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            raise SetupError("Home Assistant rejected the stored Home Keeper token. " + RESET_HINT)
        response.raise_for_status()

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
        json: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        response = self.http.post(path, json=json, data=data, headers=headers)
        if response.is_error:
            raise SetupError(f"Home Assistant refused {path} with status {response.status_code}")
        return response.json()

    def create_long_lived_token(self, access_token: str) -> str:
        scheme = "wss" if self.origin.startswith("https") else "ws"
        with connect(f"{scheme}://{self.origin.split('://', 1)[1]}/api/websocket") as socket:
            socket.recv()
            socket.send(json.dumps({"type": "auth", "access_token": access_token}))
            if WebSocketAuth.model_validate_json(socket.recv()).type != "auth_ok":
                raise SetupError("Home Assistant rejected the onboarding session")
            socket.send(
                json.dumps(
                    {
                        "id": 1,
                        "type": "auth/long_lived_access_token",
                        "client_name": "Home Keeper",
                        "lifespan": TOKEN_LIFESPAN_DAYS,
                    }
                )
            )
            reply = WebSocketResult.model_validate_json(socket.recv())
        if not reply.success or not reply.result:
            raise SetupError("Home Assistant did not create the Home Keeper token")
        return reply.result


def show_login(login_file: Path, public_url: str) -> None:
    try:
        login = Login.model_validate_json(login_file.read_text())
    except FileNotFoundError:
        raise SetupError("No login stored yet. Run `docker compose up --wait` first.") from None
    print(f"Home Assistant: {public_url or '(see HA_PORT)'}")
    print(f"Username: {login.username}")
    print(f"Password: {login.password}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token_file = Path(os.environ.get("HOME_KEEPER_HA_TOKEN_FILE", "/ha-token/token"))
    login_file = Path(os.environ.get("HOME_KEEPER_HA_LOGIN_FILE", "/ha-owner/login.json"))
    try:
        if sys.argv[1:] == ["login"]:
            show_login(login_file, os.environ.get("HOME_KEEPER_HA_PUBLIC_URL", ""))
        elif sys.argv[1:] in ([], ["setup"]):
            origin = parse_origin("HOME_KEEPER_HA_URL", os.environ.get("HOME_KEEPER_HA_URL", ""))
            if origin is None:
                raise SetupError("Set HOME_KEEPER_HA_URL to the Home Assistant origin")
            Setup(origin, token_file, login_file).run()
        else:
            raise SetupError("Usage: ha_setup [setup|login]")
    except (
        SetupError,
        httpx.HTTPError,
        WebSocketException,
        OSError,
        # Includes pydantic's ValidationError for malformed Home Assistant replies.
        ValueError,
    ) as error:
        logging.error("Home Assistant setup failed: %s", error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
