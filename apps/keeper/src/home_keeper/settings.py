"""The environment variables the API, the collectors and the Home Assistant setup read."""

from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from home_keeper_settings import OptionalOrigin, Port

MAX_INTERVAL_SECONDS = 3600


class ServiceSettings(BaseSettings):
    """What the API and both collectors share: the API's port and the service credential."""

    model_config = SettingsConfigDict(env_prefix="HOME_KEEPER_")

    port: Port = 8000
    # The API creates this credential at its first start; the others wait for it.
    token_file: Path = Path("/secrets/service-token")
    # The API and the collector must agree: the API judges freshness by this interval.
    reconciliation_seconds: int = Field(default=10, ge=1, le=MAX_INTERVAL_SECONDS)


class ApiSettings(ServiceSettings):
    db: Path = Path("/data/home.sqlite")
    source_id: str = "demo"
    bind: str = "127.0.0.1"
    # Shown on the dashboard as a link; empty means no link.
    ha_public_url: OptionalOrigin = None
    # Control stays off until a controller at this origin sends heartbeats. Empty disables it.
    controller_url: OptionalOrigin = "http://controller:8080"
    retention_days: int = Field(default=30, ge=1)
    timezone: ZoneInfo = ZoneInfo("UTC")


class CollectorSettings(ServiceSettings):
    # The connector checks the origin itself, so its failure reaches the API's health status.
    ha_url: str = ""
    ha_token_file: Path = Path("/run/secrets/ha-token")


class SetupSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOME_KEEPER_")

    ha_url: OptionalOrigin = None
    # Printed by `ha_setup login`, as the address you open in the browser.
    ha_public_url: str = ""
    ha_token_file: Path = Path("/ha-token/token")
    ha_control_token_file: Path = Path("/ha-control-token/token")
    ha_login_file: Path = Path("/ha-owner/login.json")
