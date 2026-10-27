"""Fixed Home Assistant light calls. There is no passthrough for any other domain or service."""

import json
import logging
import time
from collections.abc import Callable
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, StrictBool, TypeAdapter, ValidationError
from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from home_keeper_controller.lights import valid_token
from home_keeper_light_command import (
    ControllerUnreachable,
    LightCommand,
    LightEntityId,
    LightOff,
    LightOn,
)

# Re-exported so callers of this backend can catch its failure from here.
__all__ = ["ControllerUnreachable", "HomeAssistantLights"]

MAX_PAYLOAD = 64 * 1024
MAX_TIMEOUT_SECONDS = 30
# One deadline for connect, authentication and the call; the API waits 6 s for the answer.
COMMAND_DEADLINE_SECONDS = 4
CLOSE_TIMEOUT_SECONDS = 1
# How often the controller re-checks that Home Assistant accepts the control token.
AUTH_CHECK_SECONDS = 30
_LIGHT_ENTITY_ID: TypeAdapter[str] = TypeAdapter(LightEntityId)
# Library debug output can include the authentication frame.
_WS_LOGGER = logging.getLogger("home_keeper_controller.transport.private")
_WS_LOGGER.addHandler(logging.NullHandler())
_WS_LOGGER.propagate = False
_WS_LOGGER.setLevel(logging.CRITICAL + 1)


class WebSocket(Protocol):
    """The part of a websockets sync connection the backend uses."""

    def send(self, message: str, /) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...


class _Frame(BaseModel):
    # Home Assistant adds fields such as `ha_version`; only the named ones matter.
    model_config = ConfigDict(extra="ignore")


class AuthFrame(_Frame):
    type: Literal["auth_required", "auth_ok", "auth_invalid"]


class ResultFrame(_Frame):
    id: Literal[1]
    type: Literal["result"]
    success: StrictBool


class HomeAssistantLights:
    """Sends `light.turn_on` or `light.turn_off` over Home Assistant's websocket API."""

    def __init__(
        self,
        url: str,
        token: Callable[[], str],
        timeout_seconds: float = COMMAND_DEADLINE_SECONDS,
    ) -> None:
        """`token` reads the control token. Each connection reads it again, so a token
        that setup replaces works without a controller restart."""
        parsed = urlsplit(url)
        try:
            _ = parsed.port  # Raises ValueError for an out-of-range or non-numeric port.
        except ValueError:
            raise ValueError("Home Assistant URL must be an http(s) origin") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not url.isascii()
        ):
            raise ValueError("Home Assistant URL must be an http(s) origin")
        self._token = token
        # Read once now, so a bad token is a configuration error at start.
        self._read_token()
        if not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError("Timeout out of range")
        scheme = "wss" if parsed.scheme == "https" else "ws"
        self._websocket_url = f"{scheme}://{parsed.netloc}/api/websocket"
        self._timeout = timeout_seconds
        self._authorized = False
        self._checked_at: float | None = None

    def usable(self) -> bool:
        """Whether Home Assistant accepts the control token, re-checked every 30 seconds.

        A command whose authentication is rejected makes it false until the next check.
        """
        if self._checked_at is None or time.monotonic() - self._checked_at >= AUTH_CHECK_SECONDS:
            authorized = self._authenticates()
            # Log changes only, never the token or Home Assistant's reply.
            if authorized and (self._checked_at is None or not self._authorized):
                logging.info("Home Assistant accepts the control token; light control is on")
            elif not authorized and (self._checked_at is None or self._authorized):
                logging.warning(
                    "Light control is off: Home Assistant did not accept the control token "
                    "or did not answer"
                )
            self._authorized = authorized
            self._checked_at = time.monotonic()
        return self._authorized

    def _authenticates(self) -> bool:
        deadline = time.monotonic() + self._timeout
        try:
            with self._connect() as socket:
                return self._authenticate(socket, deadline)
        except (ControllerUnreachable, OSError, WebSocketException, ValueError):
            return False

    def _read_token(self) -> str:
        token = self._token()
        if not valid_token(token):
            raise ValueError("Home Assistant control token is invalid")
        return token

    def _connect(self) -> ClientConnection:
        return connect(
            self._websocket_url,
            open_timeout=self._timeout,
            close_timeout=CLOSE_TIMEOUT_SECONDS,
            max_size=MAX_PAYLOAD,
            proxy=None,
            logger=_WS_LOGGER,
        )

    def _authenticate(self, socket: WebSocket, deadline: float) -> bool:
        """Whether Home Assistant accepts the token on this connection."""
        if self._receive(socket, deadline, AuthFrame).type != "auth_required":
            raise ControllerUnreachable
        socket.send(json.dumps({"type": "auth", "access_token": self._read_token()}))
        return self._receive(socket, deadline, AuthFrame).type == "auth_ok"

    def send(self, source_entity_id: str, command: LightCommand) -> Literal["ok", "refused"]:
        # Checked again here, so no caller can reach Home Assistant with another entity.
        try:
            _LIGHT_ENTITY_ID.validate_python(source_entity_id)
        except ValidationError:
            return "refused"
        if isinstance(command, LightOn):
            message: dict[str, object] = {
                "id": 1,
                "type": "call_service",
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": source_entity_id},
            }
            if command.brightness_pct is not None:
                message["service_data"] = {"brightness_pct": command.brightness_pct}
        elif isinstance(command, LightOff):  # pyright: ignore[reportUnnecessaryIsInstance] -- untyped callers
            message = {
                "id": 1,
                "type": "call_service",
                "domain": "light",
                "service": "turn_off",
                "target": {"entity_id": source_entity_id},
            }
        else:
            return "refused"
        deadline = time.monotonic() + self._timeout
        try:
            with self._connect() as socket:
                if not self._authenticate(socket, deadline):
                    # Heartbeats stop until the next check sees the token accepted again.
                    self._authorized = False
                    logging.warning("Home Assistant does not accept the control token")
                    raise ControllerUnreachable
                socket.send(json.dumps(message))
                reply = self._receive(socket, deadline, ResultFrame)
        except ControllerUnreachable:
            raise
        # OSError covers timeouts and TLS failures; ValueError covers malformed frames.
        except (OSError, WebSocketException, ValueError):
            raise ControllerUnreachable from None
        return "ok" if reply.success else "refused"

    @staticmethod
    def _receive[F: _Frame](socket: WebSocket, deadline: float, frame: type[F]) -> F:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControllerUnreachable
        raw = socket.recv(timeout=remaining)
        if len(raw) > MAX_PAYLOAD:
            raise ControllerUnreachable
        return frame.model_validate_json(raw)
