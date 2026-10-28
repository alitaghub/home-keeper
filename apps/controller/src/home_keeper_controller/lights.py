"""What a controller backend can do: carry out one fixed light or switch command."""

from typing import Literal, Protocol

from home_keeper_light_command import ForwardedCommand

# Visible ASCII only: no spaces, control characters or non-ASCII in credentials.
VISIBLE_ASCII = range(33, 127)
MAX_TOKEN_LENGTH = 4096


class Devices(Protocol):
    def send(self, forwarded: ForwardedCommand) -> Literal["ok", "refused"]:
        """Carry out one command; `refused` means the backend answered and rejected it.

        Raises ControllerUnreachable when no answer arrives.
        """
        ...


def valid_token(token: str) -> bool:
    return (
        0 < len(token) <= MAX_TOKEN_LENGTH
        and token.isascii()
        and all(ord(character) in VISIBLE_ASCII for character in token)
    )
