from pathlib import Path
from threading import Thread

import httpx
from fastapi.testclient import TestClient

from home_keeper.api import create_app
from home_keeper.demo import DemoReplay, command_server, reconcile


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


def test_demo_offers_its_smart_plug_and_records_its_on_off_changes(tmp_path: Path) -> None:
    replay = DemoReplay()
    with TestClient(
        create_app(tmp_path / "demo.sqlite", "service-token"),
        headers={"Authorization": "Bearer service-token"},
    ) as client:
        reconcile(client, replay)
        catalog = {entity["id"]: entity for entity in client.get("/api/catalog").json()["entities"]}
        plug = catalog["switch.smart_plug"]
        assert (plug["supported"], plug.get("device_kind")) == (True, "switch")
        assert catalog["light.hue_room"]["reason"] == "Groups and rooms are not recorded"
        assert catalog["sensor.ungrouped"]["reason"] == (
            "Only lights, plugs and switches are supported"
        )
        assert catalog["sensor.ungrouped"].get("device_kind") is None
        selected = client.put(
            "/api/selection/switch.smart_plug",
            json={"selected": True},
            headers={"X-Home-Keeper": "1"},
        )
        assert selected.status_code == 200, selected.text
        for _ in range(7):
            reconcile(client, replay)
        events = client.get("/api/events?entity_id=switch.smart_plug").json()["events"]
        assert events
        states = {"on", "off", "unavailable", "unknown"}
        for event in events:
            assert event["device_kind"] == "switch"
            for reading in (event["old"], event["new"]):
                if reading is not None:
                    assert reading["state"] in states
                    assert "brightness" not in reading
        assert {event["new"]["state"] for event in events} >= {"on", "off"}
        [monitored] = client.get("/api/monitored").json()["entities"]
        assert monitored["baseline"]["device_kind"] == "switch"


def test_demo_switches_its_smart_plug_on_a_switch_command_and_records_the_change(
    tmp_path: Path,
) -> None:
    replay = DemoReplay()
    with TestClient(
        create_app(tmp_path / "demo.sqlite", "service-token"),
        headers={"Authorization": "Bearer service-token"},
    ) as client:
        reconcile(client, replay)
        client.put(
            "/api/selection/switch.smart_plug",
            json={"selected": True},
            headers={"X-Home-Keeper": "1"},
        ).raise_for_status()
        reconcile(client, replay)
        before = replay.states["switch.smart_plug"].state
        assert before in ("on", "off")
        after = "off" if before == "on" else "on"
        known = {event["id"] for event in client.get("/api/events").json()["events"]}

        server = command_server(client, replay, "service-token", port=0)
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            response = httpx.post(
                f"http://127.0.0.1:{server.server_address[1]}/internal/command",
                json={
                    "device_kind": "switch",
                    "source_entity_id": "switch.smart_plug",
                    "command": {"action": after},
                },
                headers={"Authorization": "Bearer service-token"},
                timeout=5,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        assert (response.status_code, response.json()) == (200, {"result": "ok"})
        assert replay.states["switch.smart_plug"].state == after
        events = client.get("/api/events?entity_id=switch.smart_plug").json()["events"]
        [new] = [event for event in events if event["id"] not in known]
        assert new["device_kind"] == "switch"
        assert (new["old"]["state"], new["new"]["state"]) == (before, after)
        assert "brightness" not in new["new"]
