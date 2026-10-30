"""The demo home's history, read by the API through the reader service as in the demo stack.

The demo shows a day of history from its first minute, and its replay and commands add to it.
"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from home_keeper.api import create_app
from home_keeper.reader import HttpReader
from home_keeper.reader_service import DemoBackend, create_reader_app

SERVICE = "demo-service-token"
AUTH = f"Bearer {SERVICE}"
DEVICES = ("light.hue_desk", "light.hue_reading", "switch.smart_plug")
COLOR_FIELDS = (
    "color_mode",
    "color_temp_kelvin",
    "hs_color",
    "xy_color",
    "rgb_color",
    "rgbw_color",
    "rgbww_color",
)


@dataclass
class Demo:
    api: TestClient
    reader: TestClient
    backend: DemoBackend

    def changes(self, **params: str) -> list[dict[str, Any]]:
        """Every change the API lists, following the cursor."""
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page_params = params | ({"cursor": cursor} if cursor else {})
            response = self.api.get("/api/events", params={"limit": 100, **page_params})
            assert response.status_code == 200, response.text
            page = response.json()
            found.extend(page["events"])
            cursor = page["next_cursor"]
            if cursor is None:
                return found
            assert len(found) < 10_000, "the cursor never ends"

    def state(self, entity_id: str) -> str:
        response = self.reader.get("/internal/states", params={"entity_id": entity_id})
        assert response.status_code == 200, response.text
        state: str = response.json()["states"][entity_id]["reading"]["state"]
        return state


@contextmanager
def demo(tmp_path: Path) -> Generator[Demo]:
    """The API reads a fresh demo reader over its internal HTTP; every demo device is
    monitored."""
    backend = DemoBackend()
    with TestClient(
        create_reader_app(backend, SERVICE),
        base_url="http://reader:8080",
        headers={"Authorization": AUTH},
    ) as reader_client:
        reader = HttpReader("http://reader:8080", SERVICE)
        reader._http = reader_client  # pyright: ignore[reportPrivateUsage] -- serve the reader app in-process
        with TestClient(create_app(tmp_path / "demo.sqlite", SERVICE, reader=reader)) as api:
            for entity_id in DEVICES:
                api.put(
                    f"/api/selection/{entity_id}",
                    json={"selected": True},
                    headers={"X-Home-Keeper": "1"},
                ).raise_for_status()
            yield Demo(api, reader_client, backend)


def kinds(change: dict[str, Any]) -> set[str]:
    old, new = change["old"], change["new"]
    found: set[str] = set()
    if "unavailable" in (old["state"], new["state"]):
        found.add("unavailable")
    elif old["state"] != new["state"]:
        found.add("on/off")
    if old["state"] == new["state"] == "on":
        if old.get("brightness") != new.get("brightness"):
            found.add("brightness")
        if any(old.get(field) != new.get(field) for field in COLOR_FIELDS):
            found.add("color")
    return found


def test_demo_history_shows_a_day_of_changes_from_the_first_minute(tmp_path: Path) -> None:
    with demo(tmp_path) as home:
        now = datetime.now(UTC)

        changes = home.changes(start=(now - timedelta(hours=24)).isoformat())

        times = [datetime.fromisoformat(change["time"]) for change in changes]
        assert any(time < now - timedelta(hours=23) for time in times), min(times, default=None)
        assert len([time for time in times if time < now - timedelta(hours=1)]) >= 23
        assert {change["entity_id"] for change in changes} == set(DEVICES)
        found = set[str]().union(*(kinds(change) for change in changes))
        assert found >= {"brightness", "color", "on/off", "unavailable"}, found
        plug = [change for change in changes if change["entity_id"] == "switch.smart_plug"]
        for change in plug:
            assert change["device_kind"] == "switch"
            assert "brightness" not in change["old"]
            assert "brightness" not in change["new"]


def test_each_replay_step_adds_to_the_demo_history(tmp_path: Path) -> None:
    with demo(tmp_path) as home:
        before = home.changes()
        stepped = datetime.now(UTC)

        home.backend.step()

        after = home.changes()
        new = [change for change in after if datetime.fromisoformat(change["time"]) >= stepped]
        assert new, "the replay step changed nothing in the history"
        assert after[len(new) :] == before


def test_a_demo_command_appears_in_the_history(tmp_path: Path) -> None:
    with demo(tmp_path) as home:
        plug = "switch.smart_plug"
        # The plug may be unavailable in its replay; the next step brings it back.
        for _ in range(7):
            if home.state(plug) in ("on", "off"):
                break
            home.backend.step()
        before = home.state(plug)
        after = "off" if before == "on" else "on"
        sent = datetime.now(UTC)

        response = home.reader.post(
            "/internal/command",
            json={"device_kind": "switch", "source_entity_id": plug, "command": {"action": after}},
        )

        assert (response.status_code, response.json()) == (200, {"result": "ok"})
        newest = home.changes(entity_id=plug)[0]
        assert datetime.fromisoformat(newest["time"]) >= sent
        assert (newest["old"]["state"], newest["new"]["state"]) == (before, after)
