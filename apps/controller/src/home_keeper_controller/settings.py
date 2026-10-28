"""Every environment variable the controller reads, with its default.

Compose sets them in compose.yaml and compose.demo.yaml.
"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from home_keeper_settings import Port


class ControllerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOME_KEEPER_")

    # The API's port; the controller reaches it on the internal control network.
    port: Port = 8000
    # The API creates this credential at its first start; the controller waits for it.
    token_file: Path = Path("/secrets/service-token")
    control_backend: Literal["home_assistant", "demo"] = "home_assistant"
    controller_port: Port = 8080
    # Inside the container only; Compose publishes no port for the controller.
    controller_bind: str = "0.0.0.0"  # noqa: S104 -- internal network only
    # Used by the home_assistant backend, which checks the origin itself.
    ha_url: str = ""
    ha_control_token_file: Path = Path("/ha-control-token/token")
