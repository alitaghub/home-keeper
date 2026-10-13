"""Run isolated live Compose against a synthetic HA, never home credentials."""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="observer-live-proof-") as directory:
        root = Path(directory)
        # This is a public fixture value, not a Home Assistant credential.
        token = root / "token"
        token.write_text("synthetic-compose-token")
        root.chmod(0o755)
        token.chmod(0o444)
        override = root / "proof.yaml"
        override.write_text(f"""services:
  api:
    ports: !override ["127.0.0.1:18001:8000"]
  collector:
    depends_on:
      fake-ha:
        condition: service_started
  fake-ha:
    build: {REPO}
    command: [python, /fixture/fake_home_assistant.py]
    volumes:
      - {REPO / "tests" / "fake_home_assistant.py"}:/fixture/fake_home_assistant.py:ro
    networks: [live]
    read_only: true
    cap_drop: [ALL]
""")
        env = os.environ | {
            "OBSERVER_HA_URL": "http://fake-ha:8123",
            "OBSERVER_HA_TOKEN_FILE": str(token),
            "OBSERVER_RECONCILIATION_SECONDS": "1",
        }
        command = [
            "docker",
            "compose",
            "-p",
            "observer-live-proof",
            "-f",
            str(REPO / "compose.yaml"),
            "-f",
            str(REPO / "compose.live.yaml"),
            "-f",
            str(override),
        ]

        def compose(*args):
            return subprocess.run(
                command + list(args),
                env=env,
                cwd=REPO,
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            ).stdout

        started = time.monotonic()
        try:
            configuration = json.loads(compose("config", "--format", "json"))
            assert not any(
                v["target"] == "/run/secrets/ha-token"
                for v in configuration["services"]["api"]["volumes"]
            )
            assert "OBSERVER_HA_URL" not in configuration["services"]["api"]["environment"]
            compose("up", "--build", "--wait")
            with httpx.Client(
                base_url="http://127.0.0.1:18001",
                headers={"Host": "localhost:8000", "X-Home-Observer": "1"},
                timeout=5,
                trust_env=False,
            ) as client:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    catalog = client.get("/api/catalog").json()
                    if len(catalog["entities"]) == 2:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("Live collector never published discovery")
                assert catalog["source_id"] == "home_assistant"
                assert client.get("/api/selection").json()["entity_ids"] == []
                client.put("/api/selection/ha:fixture", json={"selected": True}).raise_for_status()
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    monitored = client.get("/api/monitored").json()["entities"]
                    if monitored and monitored[0]["freshness"] == "fresh":
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("Selected live state never confirmed")
                assert monitored[0]["baseline"]["source_entity_id"] == "light.fixture"
                assert monitored[0]["baseline"]["state"] == "off"
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    events = client.get("/api/events").json()["events"]
                    if events:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("Subscribed live brightness event not recorded")
                assert events[0]["old"]["brightness"] != events[0]["new"]["brightness"]
                assert events[0]["source_entity_id"] == "light.fixture"
                for endpoint in ("catalog", "monitored", "health", "selection"):
                    content = client.get(f"/api/{endpoint}").text
                    assert "synthetic-compose-token" not in content
                    assert "ADMIN_SENTINEL" not in content
                assert client.get("/api/baselines?entity_id=ha:unsupported").json() == {
                    "baselines": []
                }
                compose("stop", "fake-ha")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    health = client.get("/api/health").json()
                    if health["status"] == "failed":
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("Stopped HA fixture did not cause visible failure")
                assert client.get("/api/catalog").json()["freshness"] == "stale"
                gap_state = client.get("/api/coverage").json()
                assert gap_state["state"] == "interrupted"
                interrupted_ids = {
                    gap["id"] for gap in gap_state["gaps"] if gap["recovered_at"] is None
                }
                assert interrupted_ids
                assert not any(
                    event["new"] and event["new"]["state"] == "unavailable"
                    for event in client.get("/api/events").json()["events"]
                )
                compose("start", "fake-ha")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if client.get("/api/health").json()["status"] == "healthy":
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("Live connector did not recover")
                coverage = client.get("/api/coverage").json()
                assert coverage["state"] == "collecting"
                assert all(
                    gap["recovered_at"] for gap in coverage["gaps"] if gap["id"] in interrupted_ids
                )
                snapshots = client.get("/api/baselines?entity_id=ha:fixture").json()["baselines"]
                assert any(
                    snapshot["kind"] == "recovery" and snapshot["transition_time"] == "unknown"
                    for snapshot in snapshots
                )
                compose("restart", "api")
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        coverage = client.get("/api/coverage").json()
                        if coverage["state"] == "collecting" and any(
                            gap["reason"] == "restart" and gap["recovered_at"]
                            for gap in coverage["gaps"]
                        ):
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.2)
                else:
                    raise AssertionError("API restart did not persist and recover a gap")
            logs = compose("logs", "--no-color", "api", "collector")
            assert "synthetic-compose-token" not in logs
            print(
                json.dumps(
                    {
                        "live_compose": "passed",
                        "synthetic_only": True,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }
                )
            )
        finally:
            compose("down", "--volumes")


if __name__ == "__main__":
    main()
