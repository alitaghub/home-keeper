"""Boot the default stack with the real pinned Home Assistant image and prove setup needs no human.

Runs with the `control` profile, so setup also mints the controller's own token.

Also proves that History reads Home Assistant's real recorder: the proof sets a template
light's state a few times and reads the changes back through the reader and the API.

Also proves generic state devices against the real integration: Home Assistant's `demo`
integration registers device-backed climate entities, which the catalog must list as supported
`state` devices, and a monitored climate reading carries Home Assistant's own translated word
for its state (from the real `frontend/get_translations` payload).

Also proves on/off devices: Home Assistant's `demo` integration registers device-backed binary
sensors with a device class (moisture, motion), which the catalog must list as supported
`on_off` devices, and a monitored reading carries Home Assistant's translated word for its
device class ("Wet", "Detected").

Also proves measurements against Home Assistant's long-term statistics: the `demo`
integration's outside temperature sensor has a `state_class`, so the catalog must list it as a
supported `measurement` device. The proof imports known hourly statistics for it through
`recorder/import_statistics`, one of them older than the recorder's 10-day raw history, with a
gap, and reads them back through `/api/measurements` for 7 and 30 days: exact buckets and
exact overall minimum, mean and maximum. An empty answer fails the proof.

Uses its own Compose project and ports, and deletes its volumes at the end.
"""

import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from websockets.sync.client import connect

REPO = Path(__file__).resolve().parents[1]
PROJECT = "keeper-home-proof"
HOME_KEEPER_PORT = "18002"
HA_PORT = "18124"
KEEPER = f"http://127.0.0.1:{HOME_KEEPER_PORT}"
HOME_ASSISTANT = f"http://127.0.0.1:{HA_PORT}"
# A template light: a light in Home Assistant's registry with no hardware behind it.
PROOF_NAME = "Keeper history proof"
PROOF_LIGHT = f"""
template:
  - light:
      - name: {PROOF_NAME}
        unique_id: keeper_history_proof
        turn_on: []
        turn_off: []
"""
# Home Assistant's demo integration: climate, sensors, fans and more on demo devices, with no
# hardware behind them.
PROOF_CONFIG = f"{PROOF_LIGHT}\ndemo:\n"
PROOF_ENTITY = "light.keeper_history_proof"
# What Home Assistant's English translations call each mode a demo climate entity can report.
CLIMATE_LABELS = {
    "heat": "Heat",
    "cool": "Cool",
    "off": "Off",
    "auto": "Auto",
    "heat_cool": "Heat/Cool",
    "dry": "Dry",
    "fan_only": "Fan only",
}
# What Home Assistant's English translations call the states of the device classes the demo
# binary sensors have: moisture reads Wet/Dry and motion Detected/Clear.
BINARY_SENSOR_LABELS = {
    "moisture": {"on": "Wet", "off": "Dry"},
    "motion": {"on": "Detected", "off": "Clear"},
}
# A demo sensor with a `state_class` (a temperature, in °C). Unverified against the bundled
# image: if it is absent the proof fails and lists the measurements Home Assistant does have.
MEASURED_SENSOR = "sensor.outside_temperature"
MEASURED_UNIT = "°C"
# Hourly statistics the proof imports, as (hours before the current hour, mean, min, max). One
# is 20 days old, past the recorder's default 10 days of raw history; two follow each other;
# the hours between the later ones are a gap.
STATISTICS: list[tuple[int, float, float, float]] = [
    (20 * 24, 10.0, 5.0, 20.0),
    (5 * 24, 30.0, 20.0, 45.0),
    (5 * 24 - 1, 50.0, 40.0, 60.0),
    (2 * 24, 70.0, 55.0, 95.0),
]
# The states the proof sets, in order: on, brightness only, color only, then off.
PROOF_STATES: list[dict[str, Any]] = [
    {"state": "on", "attributes": {"brightness": 100, "color_mode": "brightness"}},
    {"state": "on", "attributes": {"brightness": 200, "color_mode": "brightness"}},
    {
        "state": "on",
        "attributes": {"brightness": 200, "color_mode": "xy", "xy_color": [0.4, 0.3]},
    },
    {"state": "off", "attributes": {"friendly_name": PROOF_NAME}},
]
# What each state is as a Home Keeper reading, without its times.
PROOF_READINGS: list[dict[str, Any]] = [
    {"state": "on", "brightness": 100, "color_mode": "brightness"},
    {"state": "on", "brightness": 200, "color_mode": "brightness"},
    {"state": "on", "brightness": 200, "color_mode": "xy", "xy_color": [0.4, 0.3]},
    {"state": "off"},
]
# Asks the reader for history from inside its container, with the service credential only
# the keeper's containers hold.
READ_HISTORY = """
import sys
import httpx
token = open("/secrets/service-token").read().strip()
response = httpx.get(
    "http://127.0.0.1:8080/internal/history",
    params={"entity_id": sys.argv[1], "start": sys.argv[2], "end": sys.argv[3]},
    headers={"Authorization": f"Bearer {token}"},
    timeout=15,
)
print(response.status_code)
print(response.text)
"""


def compose(
    *args: str, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(REPO / "compose.yaml"),
            "--profile",
            "control",
            *args,
        ],
        env=os.environ | {"HOME_KEEPER_PORT": HOME_KEEPER_PORT, "HA_PORT": HA_PORT},
        cwd=REPO,
        check=check,
        capture_output=True,
        text=True,
        timeout=600,
        input=stdin,
    )


def wait_for_healthy_reader(client: httpx.Client) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if client.get("/api/health").json()["status"] == "healthy":
            return
        time.sleep(1)
    raise AssertionError("The reader never reported healthy against the bundled Home Assistant")


def wait_until_control_on(client: httpx.Client) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if client.get("/api/control").json()["state"] == "on":
            return
        time.sleep(1)
    raise AssertionError("Controller never reported control on against the bundled Home Assistant")


def log_in(username: str, password: str) -> str:
    """Run Home Assistant's own login form flow, as the browser does."""
    client_id = f"{HOME_ASSISTANT}/"
    with httpx.Client(base_url=HOME_ASSISTANT, timeout=10, trust_env=False) as client:
        flow = client.post(
            "/auth/login_flow",
            json={
                "client_id": client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": f"{client_id}?auth_callback=1",
            },
        ).json()
        return client.post(
            f"/auth/login_flow/{flow['flow_id']}",
            json={"client_id": client_id, "username": username, "password": password},
        ).json()["type"]


def current_user(token: str) -> Any:
    """Ask Home Assistant which user a token belongs to."""
    with connect(f"ws://127.0.0.1:{HA_PORT}/api/websocket") as socket:
        socket.recv()
        socket.send(json.dumps({"type": "auth", "access_token": token}))
        assert json.loads(socket.recv())["type"] == "auth_ok"
        socket.send(json.dumps({"id": 1, "type": "auth/current_user"}))
        reply = json.loads(socket.recv())
    assert reply["success"], reply
    return reply["result"]


def control_token() -> str:
    return compose(
        "run", "--rm", "--no-deps", "--entrypoint", "cat", "ha-setup", "/ha-control-token/token"
    ).stdout.strip()


def near(value: str, expected: str) -> bool:
    """Whether two Home Assistant times name the same instant. The recorder keeps Unix-second
    floats, which can differ from the state's own time in the last microsecond."""
    difference = datetime.fromisoformat(value) - datetime.fromisoformat(expected)
    return abs(difference) <= timedelta(milliseconds=1)


def proof_light(client: httpx.Client) -> str:
    """The template light's Home Keeper ID, once the reader's catalog lists it."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = client.get("/api/catalog")
        if response.status_code == 200:
            for entity in response.json()["entities"]:
                if entity["name"] == PROOF_NAME:
                    assert entity["supported"], entity
                    identity: str = entity["id"]
                    return identity
        time.sleep(1)
    raise AssertionError("The template light never appeared in the catalog")


def set_proof_states(owner_token: str) -> list[dict[str, Any]]:
    """Set each proof state through Home Assistant's REST API; return what it stored."""
    stored: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=HOME_ASSISTANT,
        headers={"Authorization": f"Bearer {owner_token}"},
        timeout=10,
        trust_env=False,
    ) as home_assistant:
        for state in PROOF_STATES:
            response = home_assistant.post(f"/api/states/{PROOF_ENTITY}", json=state)
            assert response.status_code in (200, 201), response.text
            stored.append(response.json())
            # Distinct times, so each state is its own recorder row.
            time.sleep(0.5)
    return stored


def reader_history(identity: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The reader's history of one device, read inside the reader container."""
    result = compose(
        "exec",
        "-T",
        "reader",
        "python",
        "-c",
        READ_HISTORY,
        identity,
        start.isoformat(),
        end.isoformat(),
    ).stdout.splitlines()
    assert result[0] == "200", result
    rows: list[dict[str, Any]] = json.loads(result[1])["history"][identity]
    return rows


def prove_history_comes_from_the_recorder(owner_token: str) -> None:
    """Set states on the template light, then read them back as recorder rows through the
    reader and as changes through the API, at Home Assistant's own times."""
    with httpx.Client(base_url=KEEPER, timeout=10, trust_env=False) as client:
        wait_for_healthy_reader(client)
        identity = proof_light(client)
        stored = set_proof_states(owner_token)
        start = datetime.fromisoformat(stored[0]["last_updated"]) - timedelta(seconds=1)
        end = datetime.now(UTC) + timedelta(minutes=1)

        # The recorder commits every few seconds; wait until it holds the last state.
        deadline = time.monotonic() + 60
        rows = reader_history(identity, start, end)
        while not any(
            near(row["reading"]["last_updated"], stored[-1]["last_updated"]) for row in rows
        ):
            assert time.monotonic() < deadline, rows
            time.sleep(2)
            rows = reader_history(identity, start, end)

        for state, reading in zip(stored, PROOF_READINGS, strict=True):
            [row] = [r for r in rows if near(r["reading"]["last_updated"], state["last_updated"])]
            assert row["source_entity_id"] == PROOF_ENTITY, row
            parsed = row["reading"]
            assert parsed["device_kind"] == "light", parsed
            assert parsed["availability"] == "available", parsed
            assert {key: parsed.get(key) for key in reading} == reading, parsed
            # A brightness or color change keeps the time the state last changed.
            assert near(parsed["last_changed"], state["last_changed"]), (parsed, state)
            assert "friendly_name" not in parsed, parsed
        assert rows[-1]["reading"].get("brightness") is None, rows[-1]

        selected = client.put(
            f"/api/selection/{identity}",
            json={"selected": True},
            headers={"X-Home-Keeper": "1"},
        )
        assert selected.status_code == 200, selected.text
        response = client.get(
            "/api/events", params={"entity_id": identity, "start": start.isoformat()}
        )
        assert response.status_code == 200, response.text
        # Newest first: off, the color change, the brightness change.
        off, color, brightness = response.json()["events"][:3]
        assert (off["old"]["state"], off["new"]["state"]) == ("on", "off")
        assert (color["old"].get("xy_color"), color["new"]["xy_color"]) == (None, [0.4, 0.3])
        assert (brightness["old"]["brightness"], brightness["new"]["brightness"]) == (100, 200)
        for change, state in zip((off, color, brightness), stored[:0:-1], strict=True):
            assert near(change["time"], state["last_updated"]), (change, state)


def prove_demo_climate_is_a_state_device_with_a_translated_label() -> None:
    """The demo integration's climate entity is a supported `state` device on a device, and
    once monitored its reading's label is Home Assistant's translated word for its state."""
    headers = {"X-Home-Keeper": "1"}
    with httpx.Client(base_url=KEEPER, timeout=30, trust_env=False) as client:
        wait_for_healthy_reader(client)
        deadline = time.monotonic() + 90
        while True:
            entities = client.get("/api/catalog").json()["entities"]
            devices = [e for e in entities if e["supported"] and e.get("device_kind") == "state"]
            if devices:
                break
            assert time.monotonic() < deadline, "The demo devices never appeared in the catalog"
            time.sleep(2)
        # Every generic device belongs to a device: the registry rule.
        assert all(device["device_id"] for device in devices), devices
        assert len(devices) <= 500, len(devices)

        for device in devices:
            selected = client.put(
                f"/api/selection/{device['id']}", json={"selected": True}, headers=headers
            )
            assert selected.status_code == 200, selected.text
        monitored = client.get("/api/monitored")
        assert monitored.status_code == 200, monitored.text
        climates = [
            entity
            for entity in monitored.json()["entities"]
            if (entity["source_entity_id"] or "").startswith("climate.")
        ]
        assert climates, "No demo climate entity is monitored"
        for climate in climates:
            assert climate["device_kind"] == "state", climate
            assert climate["supported"], climate
            reading = climate["state"]
            assert reading["device_kind"] == "state", reading
            # Home Assistant's own word: "Heat" for `heat`, not the raw state.
            assert reading["label"] == CLIMATE_LABELS[reading["state"]], reading
            assert reading["label"] != reading["state"], reading
            # A generic device has no attributes, such as a target temperature.
            assert "temperature" not in reading, reading


def prove_demo_binary_sensor_is_an_on_off_device_with_a_translated_label(token: str) -> None:
    """The demo integration's binary sensors, which have a device class, are supported `on_off`
    devices on a device, and once monitored a reading's label is Home Assistant's translated
    word for the device class and state."""
    headers = {"X-Home-Keeper": "1"}
    with httpx.Client(base_url=KEEPER, timeout=30, trust_env=False) as client:
        wait_for_healthy_reader(client)
        deadline = time.monotonic() + 90
        while True:
            entities = client.get("/api/catalog").json()["entities"]
            sensors = [e for e in entities if e["supported"] and e.get("device_kind") == "on_off"]
            if sensors:
                break
            assert time.monotonic() < deadline, "No demo on/off device appeared in the catalog"
            time.sleep(2)
        # Every on/off device belongs to a device: the registry rule.
        assert all(sensor["device_id"] for sensor in sensors), sensors

        for sensor in sensors:
            selected = client.put(
                f"/api/selection/{sensor['id']}", json={"selected": True}, headers=headers
            )
            assert selected.status_code == 200, selected.text
        monitored = client.get("/api/monitored")
        assert monitored.status_code == 200, monitored.text
        binary = [
            entity
            for entity in monitored.json()["entities"]
            if (entity["source_entity_id"] or "").startswith("binary_sensor.")
        ]
        assert binary, "No demo binary sensor is monitored as an on/off device"
        with httpx.Client(
            base_url=HOME_ASSISTANT,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            trust_env=False,
        ) as home_assistant:
            for sensor in binary:
                assert sensor["device_kind"] == "on_off", sensor
                assert sensor["supported"], sensor
                reading = sensor["state"]
                assert reading["device_kind"] == "on_off", reading
                assert reading["state"] in ("on", "off"), reading
                # The demo sensors have a device class: its translation names the state.
                source = home_assistant.get(f"/api/states/{sensor['source_entity_id']}").json()
                device_class = source["attributes"]["device_class"]
                assert reading["label"] == BINARY_SENSOR_LABELS[device_class][reading["state"]], (
                    device_class,
                    reading,
                )
                # A binary sensor has no attributes in its reading.
                assert "device_class" not in reading, reading


def import_statistics(token: str, rows: list[dict[str, Any]]) -> None:
    """Import hourly statistics for the measured sensor through Home Assistant's recorder.

    Home Assistant 2026.10 names the metadata field for averaged statistics `mean_type`
    (`has_mean` before it); this is unverified, so it asks with the newer name first and
    retries with the older one when Home Assistant rejects the metadata.
    """
    metadata = {
        "has_sum": False,
        "name": None,
        "source": "recorder",
        "statistic_id": MEASURED_SENSOR,
        "unit_of_measurement": MEASURED_UNIT,
        "unit_class": "temperature",
    }
    refusals: list[Any] = []
    for mean in ({"mean_type": 1}, {"has_mean": True}):
        with connect(f"ws://127.0.0.1:{HA_PORT}/api/websocket") as socket:
            socket.recv()
            socket.send(json.dumps({"type": "auth", "access_token": token}))
            assert json.loads(socket.recv())["type"] == "auth_ok"
            socket.send(
                json.dumps(
                    {
                        "id": 1,
                        "type": "recorder/import_statistics",
                        "metadata": metadata | mean,
                        "stats": rows,
                    }
                )
            )
            reply = json.loads(socket.recv())
        if reply["success"]:
            return
        refusals.append(reply)
    raise AssertionError(f"Home Assistant refused the imported statistics: {refusals}")


def read_measurements(
    client: httpx.Client, identity: str, start: datetime, end: datetime
) -> dict[str, Any]:
    response = client.get(
        "/api/measurements",
        params={"entity_id": identity, "start": start.isoformat(), "end": end.isoformat()},
    )
    assert response.status_code == 200, response.text
    [series] = response.json()["devices"]
    assert series["entity_id"] == identity, series
    return dict(series)


def prove_measurements_come_from_long_term_statistics(token: str) -> None:
    """The demo temperature sensor is a supported `measurement` with a number and a unit;
    statistics imported for it, one older than the raw history and with a gap, come back for 7
    and 30 days as the same buckets with exact overall numbers."""
    headers = {"X-Home-Keeper": "1"}
    with httpx.Client(base_url=KEEPER, timeout=30, trust_env=False) as client:
        wait_for_healthy_reader(client)
        deadline = time.monotonic() + 90
        while True:
            entities = client.get("/api/catalog").json()["entities"]
            measured = [
                e for e in entities if e["supported"] and e.get("device_kind") == "measurement"
            ]
            if measured:
                break
            assert time.monotonic() < deadline, "No demo measurement appeared in the catalog"
            time.sleep(2)
        # Every measurement belongs to a device: the registry rule.
        assert all(entity["device_id"] for entity in measured), measured

        for entity in measured:
            selected = client.put(
                f"/api/selection/{entity['id']}", json={"selected": True}, headers=headers
            )
            assert selected.status_code == 200, selected.text
        monitored = client.get("/api/monitored")
        assert monitored.status_code == 200, monitored.text
        sources = {
            entity["source_entity_id"]: entity
            for entity in monitored.json()["entities"]
            if entity["device_kind"] == "measurement"
        }
        assert MEASURED_SENSOR in sources, f"Demo measurements are {sorted(sources)}"
        sensor = sources[MEASURED_SENSOR]
        reading = sensor["state"]
        assert reading["device_kind"] == "measurement", reading
        assert isinstance(reading["value"], float), reading
        assert reading["unit"] == MEASURED_UNIT, reading
        # The reading is Home Assistant's own number, not a rounded or converted one.
        with httpx.Client(
            base_url=HOME_ASSISTANT,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            trust_env=False,
        ) as home_assistant:
            source = home_assistant.get(f"/api/states/{MEASURED_SENSOR}").json()
        assert source["attributes"]["state_class"], source
        assert reading["value"] == float(source["state"]), (reading, source)
        # A reading is not a command target.
        assert "brightness" not in reading, reading

        hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        buckets = {
            hour - timedelta(hours=back): (mean, low, high) for back, mean, low, high in STATISTICS
        }
        import_statistics(
            token,
            [
                {"start": start.isoformat(), "mean": mean, "min": low, "max": high}
                for start, (mean, low, high) in buckets.items()
            ],
        )

        # Asked up to a day ago, so the sensor's own statistics for the last hours stay out of
        # the exact numbers. The recorder writes the import within a few seconds.
        end = hour - timedelta(hours=24)
        deadline = time.monotonic() + 60
        week = read_measurements(client, sensor["id"], end - timedelta(days=7), end)
        while not week["buckets"]:
            assert time.monotonic() < deadline, "The imported statistics never came back"
            time.sleep(2)
            week = read_measurements(client, sensor["id"], end - timedelta(days=7), end)
        month = read_measurements(client, sensor["id"], end - timedelta(days=30), end)

        def shown(series: dict[str, Any]) -> dict[datetime, tuple[float, float, float]]:
            return {
                datetime.fromisoformat(bucket["start"]): (
                    bucket["mean"],
                    bucket["min"],
                    bucket["max"],
                )
                for bucket in series["buckets"]
            }

        in_week = {
            start: values for start, values in buckets.items() if start >= end - timedelta(days=7)
        }
        # Seven days: hourly buckets, and the missing hours are gaps, not zero buckets.
        assert week["period"] == "hour", week
        assert week["unit"] == MEASURED_UNIT, week
        assert shown(week) == in_week, (shown(week), in_week)
        assert len(in_week) == 3, in_week
        assert (week["min"], week["mean"], week["max"]) == (20.0, 50.0, 95.0), week
        # Thirty days adds the bucket from 20 days ago, which no raw history reaches.
        assert month["period"] == "hour", month
        assert shown(month) == buckets, (shown(month), buckets)
        assert (month["min"], month["mean"], month["max"]) == (5.0, 40.0, 95.0), month
        assert month["latest"] == reading["value"], month
        # Bucket ends are one hour after their starts.
        for bucket in month["buckets"]:
            assert datetime.fromisoformat(bucket["end"]) - datetime.fromisoformat(
                bucket["start"]
            ) == timedelta(hours=1), bucket

        # A range with no statistics is an empty chart and null numbers, never zeros.
        before = end - timedelta(days=60)
        empty = read_measurements(client, sensor["id"], before - timedelta(days=7), before)
        assert empty["buckets"] == [], empty
        assert (empty["min"], empty["mean"], empty["max"]) == (None, None, None), empty


def main() -> None:
    started = time.monotonic()
    try:
        config = json.loads(compose("config", "--format", "json").stdout)
        published = {
            name: [port["host_ip"] for port in service.get("ports", [])]
            for name, service in config["services"].items()
            if service.get("ports")
        }
        assert published == {"web": ["127.0.0.1"], "homeassistant": ["127.0.0.1"]}, published
        services = config["services"]
        assert "home-assistant" not in services["api"]["networks"]
        # The controller reaches Home Assistant and, over an internal network, only the API.
        assert set(services["controller"]["networks"]) == {"control", "home-assistant"}
        assert "control" in services["api"]["networks"]
        assert "control" not in services["reader"]["networks"]
        assert "home-assistant" in services["reader"]["networks"]
        # The reader keeps nothing: no data volume, and no published port.
        assert not any(
            volume["target"] == "/data" for volume in services["reader"].get("volumes", [])
        )
        assert services["api"]["environment"]["HOME_KEEPER_READER_URL"] == "http://reader:8080"
        assert config["networks"]["control"].get("internal") is True
        mounts = {
            (name, volume["source"], volume.get("read_only", False))
            for name, service in config["services"].items()
            for volume in service.get("volumes", [])
            if volume["source"] in {"ha-token", "ha-owner", "ha-control-token"}
        }
        assert mounts == {
            ("ha-setup", "ha-token", False),
            ("ha-setup", "ha-owner", False),
            ("ha-setup", "ha-control-token", False),
            ("reader", "ha-token", True),
            ("controller", "ha-control-token", True),
        }, mounts
        compose("up", "--build", "--wait", "--wait-timeout", "400")
        states = {
            row["Service"]: row
            for row in map(json.loads, compose("ps", "-a", "--format", "json").stdout.splitlines())
        }
        assert states["ha-setup"]["ExitCode"] == 0
        with httpx.Client(base_url=KEEPER, timeout=5, trust_env=False) as client:
            wait_for_healthy_reader(client)
            catalog = client.get("/api/catalog").json()
            assert catalog["source_id"] == "home_assistant"
            assert catalog["home_assistant_url"] == HOME_ASSISTANT

            login = dict(
                line.split(": ", 1)
                for line in compose(
                    "run", "--rm", "--no-deps", "ha-setup", "login"
                ).stdout.splitlines()
            )
            assert login["Home Assistant"] == HOME_ASSISTANT
            assert log_in(login["Username"], login["Password"]) == "create_entry"
            token = compose("exec", "-T", "reader", "cat", "/ha-token/token").stdout.strip()
            assert token
            control = control_token()
            assert control
            assert control != token
            user = current_user(control)
            assert user["name"] == "Home Keeper control"
            assert user["is_admin"] is False
            assert user["is_owner"] is False
            wait_until_control_on(client)
            for endpoint in ("catalog", "health", "selection", "monitored", "control", "commands"):
                body = client.get(f"/api/{endpoint}").text
                assert login["Password"] not in body
                assert token not in body
                assert control not in body
            logs = compose("logs").stdout
            assert token not in logs
            assert control not in logs
            assert login["Password"] not in logs

        # The API must not reach Home Assistant; only the reader, the controller and setup share
        # its network.
        unreachable = compose(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "import socket; socket.create_connection(('homeassistant', 8123), timeout=3)",
            check=False,
        )
        assert unreachable.returncode != 0

        # The proof's template light and the demo integration load with the restart below.
        compose(
            "exec",
            "-T",
            "homeassistant",
            "sh",
            "-c",
            "cat >> /config/configuration.yaml",
            stdin=PROOF_CONFIG,
        )

        # A restart removes HA's onboarding endpoint; setup must reuse both valid tokens.
        compose("stop", "reader", "homeassistant")
        compose("up", "--wait")
        assert "stored tokens are valid" in compose("logs", "ha-setup").stdout
        assert log_in(login["Username"], login["Password"]) == "create_entry"
        # Re-running setup with both tokens stored changes neither.
        assert control_token() == control
        assert compose("exec", "-T", "reader", "cat", "/ha-token/token").stdout.strip() == token

        prove_history_comes_from_the_recorder(token)
        prove_demo_climate_is_a_state_device_with_a_translated_label()
        prove_demo_binary_sensor_is_an_on_off_device_with_a_translated_label(token)
        prove_measurements_come_from_long_term_statistics(token)

        # Losing the token cannot be repaired silently: setup fails and says how to reset.
        compose("run", "--rm", "--no-deps", "--entrypoint", "rm", "ha-setup", "/ha-token/token")
        failed = compose("up", "--wait", check=False)
        assert failed.returncode != 0
        assert "docker compose down --volumes" in compose("logs", "ha-setup").stdout
    finally:
        compose("down", "--volumes", check=False)
    print(f"Bundled Home Assistant smoke passed in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
