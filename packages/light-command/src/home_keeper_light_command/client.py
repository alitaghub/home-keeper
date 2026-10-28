"""HTTP client for `/internal/command`, used by the API and the demo controller."""

from http import HTTPStatus
from typing import Literal

import httpx
from pydantic import ValidationError

from home_keeper_light_command import ControllerUnreachable, ForwardedCommand, ForwardResult


class CommandClient:
    """Sends one command with the service credential; any failure is ControllerUnreachable."""

    def __init__(self, url: str, service_token: str, timeout_seconds: float) -> None:
        self._client = httpx.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        )

    def send(self, forwarded: ForwardedCommand) -> Literal["ok", "refused"]:
        try:
            response = self._client.post(
                "/internal/command", json=forwarded.model_dump(mode="json", exclude_none=True)
            )
            if response.status_code != HTTPStatus.OK:
                raise ControllerUnreachable
            return ForwardResult.model_validate_json(response.content).result
        except (httpx.HTTPError, ValidationError):
            raise ControllerUnreachable from None
