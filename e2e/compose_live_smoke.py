"""Run isolated live Compose against a synthetic HA, never home credentials."""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
FAKE_HOME_ASSISTANT = Path(__file__).resolve().parent / "fake_home_assistant.py"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="keeper-live-proof-") as directory:
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
  reader:
    depends_on:
      fake-ha:
        condition: service_started
  fake-ha:
    build: {REPO}
    command: [python, /fixture/fake_home_assistant.py]
    volumes:
      - {FAKE_HOME_ASSISTANT}:/fixture/fake_home_assistant.py:ro
    networks: [home-assistant]
    read_only: true
    cap_drop: [ALL]
""")
        env = os.environ | {
            "HOME_KEEPER_HA_URL": "http://fake-ha:8123",
            "HOME_KEEPER_HA_PUBLIC_URL": "http://ha.example.test:8123",
            "HOME_KEEPER_HA_TOKEN_FILE": str(token),
            "HOME_KEEPER_RECONCILIATION_SECONDS": "1",
            # The proof publishes its own port, so ignore a caller's HOME_KEEPER_PORT.
            "HOME_KEEPER_PORT": "8000",
        }
        command = [
            "docker",
            "compose",
            "-p",
            "keeper-live-proof",
            "-f",
            str(REPO / "compose.yaml"),
            "-f",
            str(REPO / "compose.external.yaml"),
            "-f",
            str(override),
        ]

        def compose(*args: str) -> str:
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
            assert "HOME_KEEPER_HA_URL" not in configuration["services"]["api"]["environment"]
            compose("up", "--build", "--wait")
            with httpx.Client(
                base_url="http://127.0.0.1:18001",
                headers={"Host": "localhost:8000", "X-Home-Keeper": "1"},
                timeout=5,
                trust_env=False,
            ) as client:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    response = client.get("/api/catalog")
                    if response.status_code == 200:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("The reader never served the live catalog")
                catalog = response.json()
                assert len(catalog["entities"]) == 2
                assert catalog["source_id"] == "home_assistant"
                assert catalog["home_assistant_url"] == "http://ha.example.test:8123"
                assert client.get("/api/selection").json()["entity_ids"] == []
                client.put("/api/selection/ha:fixture", json={"selected": True}).raise_for_status()
                # Read through the reader on this request: no collection pass to wait for.
                [monitored] = client.get("/api/monitored").json()["entities"]
                assert monitored["source_entity_id"] == "light.fixture"
                assert monitored["state"]["state"] == "off"
                assert monitored["missing"] is False
                for endpoint in ("catalog", "monitored", "health", "selection"):
                    content = client.get(f"/api/{endpoint}").text
                    assert "synthetic-compose-token" not in content
                    assert "ADMIN_SENTINEL" not in content
                assert client.get("/api/health").json()["status"] == "healthy"

                compose("stop", "fake-ha")
                health = client.get("/api/health").json()
                assert health["status"] == "unreachable", health
                assert health["error"]
                unavailable = client.get("/api/catalog")
                assert unavailable.status_code == 503, unavailable.text
                assert set(unavailable.json()) == {"error", "message"}
                [unknown] = client.get("/api/monitored").json()["entities"]
                assert (unknown["id"], unknown["state"]) == ("ha:fixture", None)

                compose("start", "fake-ha")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if client.get("/api/health").json()["status"] == "healthy":
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("The reader did not reach Home Assistant again")
                [recovered] = client.get("/api/monitored").json()["entities"]
                assert recovered["state"]["state"] == "off"

                # The selection is Home Keeper's own record and survives an API restart.
                compose("restart", "api")
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        if client.get("/api/selection").json()["entity_ids"] == ["ha:fixture"]:
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.2)
                else:
                    raise AssertionError("The selection did not survive an API restart")
            logs = compose("logs", "--no-color", "api", "reader")
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
