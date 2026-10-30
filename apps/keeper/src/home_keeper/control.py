"""The controller the API forwards accepted commands to, over the internal `control` network."""

from typing import Literal, Protocol

from home_keeper_light_command import ControllerUnreachable, ForwardedCommand
from home_keeper_light_command.client import CommandClient

# The API catches the controller's own exception, re-exported from here.
__all__ = ["Controller", "ControllerUnreachable", "HttpController"]

# Longer than the controller's own deadline, so a slow success is not read as unreachable.
CONTROLLER_TIMEOUT_SECONDS = 6


class Controller(Protocol):
    def send(self, forwarded: ForwardedCommand, timeout_seconds: float) -> Literal["ok", "refused"]:
        """`refused`: Home Assistant rejected the call. Raises ControllerUnreachable, also
        when the send takes longer than `timeout_seconds`."""
        ...


class HttpController(CommandClient):
    def __init__(self, url: str, service_token: str) -> None:
        super().__init__(url, service_token, CONTROLLER_TIMEOUT_SECONDS)
