from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from home_observer.api import create_app
from home_observer.demo import CATALOG, DemoReplay, coverage, reconcile


def test_demo_gap_resumes_with_snapshot_without_inventing_missing_transition(
    tmp_path: Path,
) -> None:
    path = tmp_path / "demo.sqlite"
    replay = DemoReplay()
    entity = "light.hue_desk"
    headers = {"Authorization": "Bearer demo-proof-token"}
    with TestClient(create_app(path, "demo-proof-token"), headers=headers) as client:
        client.post("/internal/catalog", json=CATALOG).raise_for_status()
        client.put(
            f"/api/selection/{entity}", json={"selected": True}, headers={"X-Home-Observer": "1"}
        ).raise_for_status()
        reconcile(client, replay)
        assert client.get("/api/monitored").json()["entities"][0]["baseline"]["state"] == "off"
        coverage(client, "interrupted")
        gap = client.get("/api/coverage").json()["gaps"][0]
        # The synthetic home changes while collection is paused; no event is forwarded.
        replay.advance(datetime.now(UTC))
        reconcile(client, replay)
        events = client.get("/api/events").json()["events"]
        assert len(events) == 2
        assert not any(e["old"]["state"] == "off" and e["new"]["state"] == "on" for e in events)
        snapshots = client.get(f"/api/baselines?entity_id={entity}").json()["baselines"]
        recovered = next(s for s in snapshots if s["kind"] == "recovery")
        assert recovered["state"] == "on"
        assert recovered["transition_time"] == "unknown"
        assert recovered["coverage_gap_ids"] == [gap["id"]]
        result = client.get("/api/coverage").json()
        assert result["gaps"][0]["recovered_at"] is not None
        assert result["state"] == "collecting"
    with TestClient(create_app(path, "demo-proof-token"), headers=headers) as client:
        result = client.get("/api/coverage").json()
        assert result["state"] == "interrupted"
        assert result["gaps"][0]["reason"] == "restart"
        assert client.get("/api/events").json()["events"] == events
        reconcile(client, replay)
        assert client.get("/api/coverage").json()["state"] == "collecting"
