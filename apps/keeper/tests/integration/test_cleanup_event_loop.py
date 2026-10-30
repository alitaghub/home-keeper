"""A lock on an expired command must not freeze every other request."""

import threading
import time

import anyio
import httpx
import psycopg
import pytest
from support.api import NOW, app_for

pytestmark = pytest.mark.anyio

HOLD_SECONDS = 3.0


async def test_retention_cleanup_does_not_block_the_event_loop(database: str) -> None:
    app = app_for(database)
    async with app.router.lifespan_context(app):
        with psycopg.connect(database, autocommit=True) as seed:
            seed.execute(
                "INSERT INTO commands VALUES('old', 'bulb-a', %s, '{}'::jsonb)",
                (NOW.replace(year=2025),),
            )
        # Another session holds the expired row, as a long transaction or an operator would.
        locker = psycopg.connect(database)
        locker.execute("SELECT * FROM commands WHERE id='old' FOR UPDATE")
        threading.Timer(HOLD_SECONDS, locker.commit).start()

        gaps: list[float] = []

        async def tick() -> None:
            last = time.monotonic()
            while True:
                await anyio.sleep(0.05)
                current = time.monotonic()
                gaps.append(current - last)
                last = current

        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(tick)
            await anyio.sleep(0.2)
            await client.get("/api/selection")
            tasks.cancel_scope.cancel()
        locker.close()

    # The ticker must keep running while the cleanup waits for the lock.
    assert max(gaps) < 1.0, f"event loop stalled for {max(gaps):.2f}s"
