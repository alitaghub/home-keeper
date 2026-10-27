"""What a controller backend can do: carry out one fixed light command."""

from typing import Literal, Protocol

from home_keeper_light_command import LightCommand

# Visible ASCII only: no spaces, control characters or non-ASCII in credentials.
VISIBLE_ASCII = range(33, 127)
MAX_TOKEN_LENGTH = 4096


class Lights(Protocol):
    def send(self, source_entity_id: str, command: LightCommand) -> Literal["ok", "refused"]:
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
