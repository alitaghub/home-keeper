"""Load a service's environment into a typed settings model, and the types they share."""

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, BeforeValidator, Field, ValidationError
from pydantic_settings import BaseSettings

Port = Annotated[int, Field(ge=1, le=65535)]


def parse_origin(value: str) -> str:
    """Return an http(s) origin such as http://localhost:3000, or raise ValueError."""
    # The message never repeats the value: a mistyped URL can carry credentials.
    error = ValueError("must be an http(s) origin without credentials, path, query or fragment")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise error from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise error
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def _blank_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


Origin = Annotated[str, AfterValidator(parse_origin)]
# An empty variable means "none configured", even when the field's default is an origin.
OptionalOrigin = Annotated[Origin | None, BeforeValidator(_blank_to_none)]


class SettingsError(ValueError):
    """Names each invalid variable and why, never the value it holds."""

    def __init__(self, problems: dict[str, str]) -> None:
        self.variables = tuple(problems)
        super().__init__(
            "; ".join(f"{variable}: {reason}" for variable, reason in problems.items())
        )


def load[T: BaseSettings](settings: type[T]) -> T:
    """Read `settings` from the environment, or raise SettingsError naming what is wrong."""
    try:
        return settings()
    except ValidationError as error:
        prefix = str(settings.model_config.get("env_prefix", ""))
        problems: dict[str, str] = {}
        for issue in error.errors(include_url=False, include_input=False):
            field = str(issue["loc"][0]) if issue["loc"] else ""
            problems.setdefault(f"{prefix}{field}".upper(), issue["msg"])
        raise SettingsError(problems) from None
