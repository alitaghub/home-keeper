"""Commands that wait for a controller send slot behind four stalled sends."""

import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from support.api import AUTH, BROWSER, CATALOG, NOW, TOKEN, observation, select
from support.control import DASHBOARD, DESK, ON, FakeController, beat

import home_keeper.api as api_module
from home_keeper.api import create_app

SLOTS = api_module.CONCURRENT_SENDS


def run_with_full_slots(
    tmp_path: Path,
    while_queued: Callable[[httpx.AsyncClient], Awaitable[None]],
) -> tuple[FakeController, list[httpx.Response]]:
    """Stalls SLOTS sends, queues one more command, runs `while_queued`, then releases all."""
    clock = [NOW]
    controller = FakeController(clock)
    app = create_app(
        tmp_path / "commands.sqlite",
        TOKEN,
        now=lambda: clock[0],
        controller=controller,
        command_wait_seconds=0.1,
        control_stale_seconds=15,
    )
    responses: list[httpx.Response] = []
    with TestClient(app) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH).raise_for_status()
        revision = select(client)
        client.post(
            "/internal/baselines",
            json=observation(
                revision, NOW.isoformat(), entity_id="bulb-a", source_entity_id=DESK, state="off"
            ),
            headers=AUTH,
        ).raise_for_status()
        beat(client, NOW)
        release = threading.Event()
        controller.on_send = lambda: release.wait(10)

        async def main() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as api:

                async def send_command() -> None:
                    responses.append(
                        await api.post(
                            "/api/devices/bulb-a/command",
                            json={"command": ON, "sender": DASHBOARD},
                            headers=BROWSER,
                            timeout=30,
                        )
                    )

                async with anyio.create_task_group() as group:
                    for _ in range(SLOTS):
                        group.start_soon(send_command)
                    deadline = time.monotonic() + 5
                    # Worker threads fill `sent`, so there is no event to wait on.
                    while len(controller.sent) < SLOTS and time.monotonic() < deadline:  # noqa: ASYNC110 -- polled thread state
                        await anyio.sleep(0.05)
                    assert len(controller.sent) == SLOTS
                    group.start_soon(send_command)
                    await anyio.sleep(0.2)
                    await while_queued(api)
                    release.set()

        anyio.run(main)
        controller.join()
    return controller, responses


def test_queued_command_is_not_sent_after_its_light_stops_being_monitored(
    tmp_path: Path,
) -> None:
    async def stop_monitoring(api: httpx.AsyncClient) -> None:
        response = await api.put("/api/selection/bulb-a", json={"selected": False}, headers=BROWSER)
        assert response.is_success, response.text

    controller, responses = run_with_full_slots(tmp_path, stop_monitoring)

    assert len(controller.sent) == SLOTS
    refusals = [response.json() for response in responses if response.status_code == 409]
    assert [refusal["detail"]["code"] for refusal in refusals] == ["not_monitored"]


def test_command_that_waits_too_long_for_a_send_slot_is_refused_as_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "SEND_QUEUE_SECONDS", 0.3)

    async def outwait_the_queue(_api: httpx.AsyncClient) -> None:
        await anyio.sleep(0.5)

    controller, responses = run_with_full_slots(tmp_path, outwait_the_queue)

    assert len(controller.sent) == SLOTS
    refusals = [response.json() for response in responses if response.status_code == 409]
    assert [refusal["detail"]["code"] for refusal in refusals] == ["busy"]
