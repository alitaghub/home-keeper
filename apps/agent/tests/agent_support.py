"""Fakes for the agent suites: the keeper API behind httpx.MockTransport, and a scripted model."""

import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

import httpx
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from home_keeper_agent.keeper import KeeperClient
from home_keeper_agent.tools import keeper_tools

# The keeper's API contract snapshots: real response bodies, so a drift in the keeper's
# responses breaks the agent's parsing here instead of in a chat.
SNAPSHOTS = Path(__file__).resolve().parents[2] / "keeper/tests/integration/snapshots/api"
HOME_KEEPER_URL = "http://keeper.test:8000"
READ_TOOLS = {
    "list_monitored",
    "get_usage",
    "get_events",
    "get_health",
    "list_devices",
}
# Writes change only the keeper's selection, never a device.
WRITE_TOOLS = {"monitor", "stop_monitoring"}


def snapshot(name: str) -> tuple[int, object]:
    """Return a recorded keeper response as (status, body).

    The snapshots replace random IDs with `<uuid-N>`; put real UUIDs back so the fake keeper
    serves what the real one does.
    """
    text = re.sub(
        r"<uuid-(\d+)>",
        lambda match: str(uuid.UUID(int=int(match.group(1)))),
        (SNAPSHOTS / f"{name}.json").read_text(),
    )
    recorded: dict[str, Any] = json.loads(text)
    return recorded["status"], recorded["body"]


@dataclass
class FakeKeeper:
    """Serves canned responses by path and records every request the agent makes."""

    responses: dict[str, tuple[int, object]] = field(default_factory=dict[str, tuple[int, object]])
    failure: httpx.TransportError | None = None
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        status, body = self.responses.get(request.url.path, (404, {"detail": "Not Found"}))
        if isinstance(body, bytes):
            # Raw JSON text, for bodies json.dumps cannot write (such as 1e999).
            return httpx.Response(
                status, content=body, headers={"content-type": "application/json"}
            )
        return httpx.Response(status, json=body)

    def client(self) -> KeeperClient:
        return KeeperClient(HOME_KEEPER_URL, transport=httpx.MockTransport(self.handle))

    def tools(self) -> dict[str, BaseTool]:
        return {tool.name: tool for tool in keeper_tools(self.client())}

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def call(tools: dict[str, BaseTool], name: str, args: Mapping[str, object]) -> str:
    result = tools[name].invoke(dict(args))
    assert isinstance(result, str), f"{name} returned {type(result).__name__}, not text"
    return result


class ScriptedChatModel(FakeMessagesListChatModel):
    """Replays scripted AI messages in order; the agent binds tools, which a script ignores."""

    @override
    def bind_tools(
        self,
        # The base signature takes Any-typed tool dicts and kwargs; a script ignores both.
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self


def lights_response(
    state: str | None = "on",
    brightness: int | None = None,
    *,
    entity_id: str = "bulb-a",
    extra_ids: Sequence[str] = (),
) -> tuple[int, object]:
    """A /api/monitored response with one light in `state` (None: Home Assistant reports no
    state, as while it cannot be reached).

    The state comes from the recorded response so its shape stays the keeper's.
    """
    _, body = snapshot("monitored")
    entities: dict[str, Any] = json.loads(json.dumps(body))
    light, missing = entities["entities"]
    if state is None:
        light["state"] = None
    else:
        light["state"] |= {"state": state, "brightness": brightness}
    light |= {"id": entity_id, "name": entity_id}
    others = [missing | {"id": other, "name": other} for other in extra_ids]
    return 200, {"entities": [light, *others]}


# Fields only a light's reading carries; the keeper's switch reading has none of them.
LIGHT_ATTRIBUTES = frozenset(
    {
        "brightness",
        "color_mode",
        "color_temp_kelvin",
        "hs_color",
        "xy_color",
        "rgb_color",
        "rgbw_color",
        "rgbww_color",
    }
)


def switch_response(state: str | None = "off", *, entity_id: str = "plug-a") -> tuple[int, object]:
    """A /api/monitored response with one monitored plug or switch in `state`.

    The recorded light's shape with `device_kind` "switch" and no light attributes.
    """
    status, recorded = lights_response(state, entity_id=entity_id)
    body: dict[str, Any] = json.loads(json.dumps(recorded))
    [switch] = body["entities"]
    switch["device_kind"] = "switch"
    if switch["state"] is not None:
        reading = {
            key: value for key, value in switch["state"].items() if key not in LIGHT_ATTRIBUTES
        }
        switch["state"] = reading | {"device_kind": "switch"}
    return status, body


def control_response(state: str) -> tuple[int, object]:
    return snapshot(f"control_{state}")


def command_response(
    outcome: str, action: str = "off", brightness_pct: int | None = None
) -> tuple[int, object]:
    """A recorded command record, with the outcome and command the test needs."""
    _, body = snapshot("command_observed")
    record: dict[str, Any] = json.loads(json.dumps(body))
    command: dict[str, Any] = {"action": action}
    if brightness_pct is not None:
        command["brightness_pct"] = brightness_pct
    record |= {"outcome": outcome, "command": command}
    return 200, record


def command_requests(keeper: FakeKeeper) -> list[httpx.Request]:
    """The requests that ask the keeper to switch a light, plug or switch."""
    return [request for request in keeper.requests if request.url.path.endswith("/command")]
