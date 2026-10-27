"""HTTP client for the keeper's public API, parsed into small local models.

They copy only the fields the tools use, since the keeper is a separate app.
"""

import os
from typing import Annotated, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Wire(BaseModel):
    # JSON like 1e999 parses to infinity; the keeper never sends it.
    model_config = ConfigDict(allow_inf_nan=False)


class KeeperError(Exception):
    """The keeper could not be reached or did not answer as expected."""


class Reading(Wire):
    state: str
    brightness: int | None = None


class MonitoredLight(Wire):
    id: str
    name: str
    device_name: str | None = None
    supported: bool
    reason: str | None = None
    baseline: Reading | None
    freshness: Literal["pending", "fresh", "stale"]
    confirmed_at: str | None


class Monitored(Wire):
    entities: list[MonitoredLight]


class UnknownInterval(Wire):
    start: str
    end: str
    reason: str


class DayUsage(Wire):
    date: str
    on_seconds: float
    unknown_seconds: float


class LightUsage(Wire):
    entity_id: str
    on_seconds: float
    unknown_seconds: float
    switch_ons: int
    average_brightness: float | None
    days: list[DayUsage]
    # On-seconds per local hour of day, 0 to 23.
    hours: list[float] = Field(min_length=24, max_length=24)
    unknown_intervals: list[UnknownInterval]


class Usage(Wire):
    start: str
    end: str
    timezone: str
    retained_since: str
    lights: list[LightUsage]


class Event(Wire):
    entity_id: str
    event_time: str
    old: Reading | None
    new: Reading | None


class EventsPage(Wire):
    events: list[Event]
    next_cursor: str | None
    retained_since: str


class Gap(Wire):
    entity_ids: list[str]
    last_confirmed_at: str | None
    uncertain_start: bool
    detected_at: str
    recovered_at: str | None
    reason: str


class CoverageReport(Wire):
    gaps: list[Gap]
    state: str
    last_confirmed_at: str | None
    retained_since: str


class Health(Wire):
    status: Literal["pending", "healthy", "failed", "stale"]
    error: str | None = None
    received_at: str | None = None


class Device(Wire):
    id: str
    name: str
    device_name: str | None = None
    supported: bool
    reason: str | None = None


class Catalog(Wire):
    refreshed_at: str | None
    freshness: Literal["pending", "fresh", "stale"]
    entities: list[Device]


class Selection(Wire):
    entity_ids: list[str]


class DiscoveryRefresh(Wire):
    refresh_revision: int


class Control(Wire):
    state: Literal["on", "off"]


class LightOn(Wire):
    action: Literal["on"] = "on"
    brightness_pct: int | None = None


class LightOff(Wire):
    action: Literal["off"] = "off"


class BrowserChatSender(Wire):
    kind: Literal["browser_chat"] = "browser_chat"


class TelegramSender(Wire):
    kind: Literal["telegram"]
    id: int
    name: str


# Who asked for a command: a label for the keeper's audit, not authentication.
Sender = Annotated[BrowserChatSender | TelegramSender, Field(discriminator="kind")]


class CommandRequest(Wire):
    command: LightOn | LightOff
    sender: Sender


class CommandRecord(Wire):
    # The keeper answers a command only after it knows one of these outcomes.
    outcome: Literal["observed", "not_observed", "refused", "unreachable"]


class Conflict(Wire):
    code: str
    message: str


class ErrorBody(Wire):
    detail: str | Conflict


type Params = dict[str, str | int | list[str]]


class KeeperClient:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url
        # The keeper only accepts writes carrying this header; reads send it too.
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            headers={"X-Home-Keeper": "1"},
            timeout=10,
        )

    @classmethod
    def from_env(cls) -> Self:
        return cls(os.environ.get("HOME_KEEPER_URL", "http://api:8000"))

    def request[T: BaseModel](
        self,
        method: Literal["GET", "POST", "PUT"],
        path: str,
        model: type[T],
        *,
        params: Params | None = None,
        body: dict[str, bool] | BaseModel | None = None,
    ) -> T:
        # Unset optional fields, such as a brightness, stay out of the body.
        payload = (
            body.model_dump(mode="json", exclude_none=True) if isinstance(body, BaseModel) else body
        )
        try:
            response = self._http.request(method, path, params=params, json=payload)
        except httpx.HTTPError:
            raise KeeperError(f"cannot reach Home Keeper at {self.base_url}") from None
        # Any non-2xx, including a redirect, is not the answer the tool asked for.
        if not response.is_success:
            raise KeeperError(_failure(response))
        try:
            return model.model_validate_json(response.content)
        except ValidationError:
            raise KeeperError(f"unexpected response from {path}") from None


def _failure(response: httpx.Response) -> str:
    message = f"HTTP {response.status_code}"
    try:
        detail = ErrorBody.model_validate_json(response.content).detail
    except ValidationError:
        return message
    # The keeper's error details are fixed strings it writes itself.
    return f"{message}: {detail if isinstance(detail, str) else detail.message}"
