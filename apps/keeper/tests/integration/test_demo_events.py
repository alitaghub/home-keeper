from pathlib import Path

from fastapi.testclient import TestClient

from home_keeper.api import create_app
from home_keeper.demo import DemoReplay, reconcile


def test_demo_records_selected_repeatable_transitions_and_deduplicates(tmp_path: Path) -> None:
    replay = DemoReplay()
    with TestClient(
        create_app(tmp_path / "demo.sqlite", "service-token"),
        headers={"Authorization": "Bearer service-token"},
    ) as client:
        reconcile(client, replay)
        assert client.get("/api/events").json()["events"] == []
        for entity in ("light.hue_desk", "light.hue_reading"):
            client.put(
                f"/api/selection/{entity}",
                json={"selected": True},
                headers={"X-Home-Keeper": "1"},
            ).raise_for_status()
        for _ in range(7):
            reconcile(client, replay)
        events = client.get("/api/events").json()["events"]
        assert len(events) == 14
        assert len({event["id"] for event in events}) == 14
        assert all(event["source_id"] == "demo" for event in events)
        for entity in ("light.hue_desk", "light.hue_reading"):
            history = [event for event in reversed(events) if event["entity_id"] == entity]
            assert [event["new"]["state"] for event in history] == [
                "on",
                "on",
                "on",
                "on",
                "unavailable",
                "on",
                "off",
            ]
            assert history[1]["new"]["brightness"] == 40
            assert history[2]["new"]["color_temp_kelvin"] == 2700
            assert history[3]["new"]["xy_color"] == [0.4, 0.3]
        assert client.get("/api/events?entity_id=switch.smart_plug").json()["events"] == []
