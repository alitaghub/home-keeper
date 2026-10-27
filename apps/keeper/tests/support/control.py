"""A stand-in light controller and request builders for the light command API."""

from collections.abc import Callable
from datetime import datetime, timedelta
from threading import Thread
from typing import Literal

import httpx
from fastapi.testclient import TestClient
from pydantic_core import to_jsonable_python

from home_keeper.control import ControllerUnreachable
from support.api import AUTH, BROWSER, Payload

ON: Payload = {"action": "on"}
OFF: Payload = {"action": "off"}
DASHBOARD: Payload = {"kind": "dashboard"}
BROWSER_CHAT: Payload = {"kind": "browser_chat"}
TELEGRAM: Payload = {"kind": "telegram", "id": 424242, "name": "Ali <b>Telegram</b>"}
# The Home Assistant entity the test readings attribute to `bulb-a`.
DESK = "light.desk"

# Called with the clock time after the send; posts the readings the light reports.
type Delivery = Callable[[datetime], object]


def on(brightness_pct: int) -> Payload:
    return {"action": "on", "brightness_pct": brightness_pct}


def command(
    client: TestClient,
    action: Payload,
    sender: Payload = DASHBOARD,
    entity: str = "bulb-a",
) -> httpx.Response:
    return client.post(
        f"/api/lights/{entity}/command",
        json={"command": action, "sender": sender},
        headers=BROWSER,
    )


def beat(client: TestClient, at: datetime) -> None:
    """Post one controller heartbeat received at `at`."""
    client.post(
        "/internal/control", json={"received_at": at.isoformat()}, headers=AUTH
    ).raise_for_status()


class FakeController:
    """Records each forwarded command and answers like the controller service.

    A send advances the shared test clock by one second, so a reading delivered at the
    new time is received after the send. Deliveries run on their own thread, as the
    collector's posts do, and `join` re-raises anything they failed with.
    """

    def __init__(self, clock: list[datetime]) -> None:
        self.clock = clock
        self.sent: list[tuple[str, object]] = []
        self.sent_at: list[datetime] = []
        self.reply: Literal["ok", "refused"] = "ok"
        self.unreachable = False
        self.deliver: Delivery | None = None
        # Runs inside send, while the API waits for the controller.
        self.on_send: Callable[[], object] | None = None
        self._threads: list[Thread] = []
        self._errors: list[BaseException] = []

    def send(self, source_entity_id: str, command: object) -> Literal["ok", "refused"]:
        self.sent.append((source_entity_id, to_jsonable_python(command, exclude_none=True)))
        self.sent_at.append(self.clock[0])
        self.clock[0] += timedelta(seconds=1)
        if self.on_send is not None:
            self.on_send()
        if self.unreachable:
            raise ControllerUnreachable
        if self.deliver is not None:
            thread = Thread(target=self._run, args=(self.deliver, self.clock[0]))
            thread.start()
            self._threads.append(thread)
        return self.reply

    def _run(self, deliver: Delivery, at: datetime) -> None:
        try:
            deliver(at)
        except BaseException as error:
            # The request thread cannot see this one; join() re-raises it in the test.
            self._errors.append(error)

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=10)
        self._threads.clear()
        if self._errors:
            raise self._errors[0]
