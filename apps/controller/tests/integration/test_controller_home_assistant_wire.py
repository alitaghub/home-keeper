"""Controller to Home Assistant over a real local websocket: the only calls it can make."""

import json
import os
import socket
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from threading import enumerate as threads
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import ServerConnection, serve

from home_keeper.api import create_app as create_api
from home_keeper.control import ControllerUnreachable, HttpController
from home_keeper.models import LightCommand
from home_keeper_controller import server
from home_keeper_controller.home_assistant import HomeAssistantLights
from home_keeper_controller.server import backend, create_app

HA_TOKEN = "synthetic-control-token"
SERVICE_TOKEN = "synthetic-service-token"
SERVICE = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
COMMAND: TypeAdapter[LightCommand] = TypeAdapter(LightCommand)

# Raw Home Assistant frames are untyped JSON.
type Frame = dict[str, Any]


def light_command(value: dict[str, object]) -> LightCommand:
    return COMMAND.validate_python(value)


class FakeHomeAssistant:
    """Authenticates the control token and answers service calls like Home Assistant."""

    def __init__(self, *, success: bool = True, delay: float = 0) -> None:
        self.success = success
        # Seconds before each frame Home Assistant sends: a slow but working instance.
        self.delay = delay
        self.connections: list[list[Frame]] = []
        self.paths: list[str] = []
        self.stopping = Event()

    def handler(self, connection: ServerConnection) -> None:
        frames: list[Frame] = []
        self.connections.append(frames)
        self.paths.append(connection.request.path if connection.request else "")
        try:
            time.sleep(self.delay)
            connection.send(json.dumps({"type": "auth_required", "ha_version": "2026.10.1"}))
            while not self.stopping.is_set():
                try:
                    frame: Frame = json.loads(connection.recv(timeout=0.1))
                except TimeoutError:
                    continue
                frames.append(frame)
                reply = self.answer(frame)
                time.sleep(self.delay)
                connection.send(json.dumps(reply))
                if reply["type"] == "auth_invalid":
                    return
        except ConnectionClosed:
            return

    def answer(self, frame: Frame) -> Frame:
        if frame.get("type") == "auth":
            if frame.get("access_token") == HA_TOKEN:
                return {"type": "auth_ok", "ha_version": "2026.10.1"}
            return {"type": "auth_invalid", "message": "Invalid access token or password"}
        if frame.get("type") == "ping":
            return {"id": frame.get("id"), "type": "pong"}
        if frame.get("type") == "call_service":
            if not self.success:
                return {
                    "id": frame.get("id"),
                    "type": "result",
                    "success": False,
                    "error": {
                        "code": "service_validation_error",
                        "message": "Light is not reachable",
                    },
                }
            context = {"id": "context", "parent_id": None, "user_id": "control-user"}
            result = {"context": context, "response": None}
            return {"id": frame.get("id"), "type": "result", "success": True, "result": result}
        return {"id": frame.get("id"), "type": "result", "success": True, "result": None}

    def calls(self) -> list[Frame]:
        """Frames after authentication, without message IDs; absent service data reads as {}."""
        calls: list[Frame] = []
        for frames in self.connections:
            assert frames[:1] in ([], [{"type": "auth", "access_token": HA_TOKEN}]), frames
            for frame in frames[1:]:
                # Feature negotiation and keep-alive change nothing in Home Assistant.
                if frame.get("type") in ("supported_features", "ping"):
                    continue
                assert isinstance(frame.get("id"), int), frame
                normalized = {key: value for key, value in frame.items() if key != "id"}
                if normalized.get("type") == "call_service":
                    normalized.setdefault("service_data", {})
                calls.append(normalized)
        return calls


@contextmanager
def running(home: FakeHomeAssistant) -> Generator[str]:
    """Serve `home` on a free local port and yield its Home Assistant origin."""
    with serve(home.handler, "127.0.0.1", 0) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.socket.getsockname()[1]}"
        finally:
            home.stopping.set()
            server.shutdown()
            thread.join(timeout=5)


def closed_origin() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def call(service: str, entity: str, **data: object) -> Frame:
    return {
        "type": "call_service",
        "domain": "light",
        "service": service,
        "service_data": data,
        "target": {"entity_id": entity},
    }


def test_light_commands_become_the_two_fixed_service_calls() -> None:
    home = FakeHomeAssistant()
    with running(home) as origin:
        lights = HomeAssistantLights(origin, HA_TOKEN)
        results = [
            lights.send("light.desk", light_command({"action": "on", "brightness_pct": 30})),
            lights.send("light.reading", light_command({"action": "on"})),
            lights.send("light.desk", light_command({"action": "off"})),
        ]
    assert results == ["ok", "ok", "ok"]
    assert home.calls() == [
        call("turn_on", "light.desk", brightness_pct=30),
        call("turn_on", "light.reading"),
        call("turn_off", "light.desk"),
    ]
    assert set(home.paths) == {"/api/websocket"}


def test_home_assistant_rejecting_the_call_is_refused() -> None:
    home = FakeHomeAssistant(success=False)
    with running(home) as origin:
        result = HomeAssistantLights(origin, HA_TOKEN).send(
            "light.desk", light_command({"action": "off"})
        )
    assert result == "refused"
    assert home.calls() == [call("turn_off", "light.desk")]


def test_unreachable_home_assistant_raises_unreachable() -> None:
    with pytest.raises(ControllerUnreachable):
        HomeAssistantLights(closed_origin(), HA_TOKEN).send(
            "light.desk", light_command({"action": "on"})
        )


def test_rejected_control_token_raises_unreachable_and_calls_nothing() -> None:
    home = FakeHomeAssistant()
    with running(home) as origin, pytest.raises(ControllerUnreachable):
        HomeAssistantLights(origin, "revoked-token").send(
            "light.desk", light_command({"action": "on"})
        )
    assert all(frame.get("type") == "auth" for frames in home.connections for frame in frames)


def test_command_server_forwards_only_light_commands_to_home_assistant() -> None:
    home = FakeHomeAssistant()
    with running(home) as origin:
        app = create_app(SERVICE_TOKEN, HomeAssistantLights(origin, HA_TOKEN))
        with TestClient(app) as client:
            for source, command in (
                ("switch.plug", {"action": "on"}),
                ("light.desk", {"action": "toggle"}),
                ("light.desk", {"action": "on", "service": "turn_on"}),
            ):
                refused = client.post(
                    "/internal/command",
                    json={"source_entity_id": source, "command": command},
                    headers=SERVICE,
                )
                assert refused.status_code == 422, refused.text
            assert home.calls() == []
            accepted = client.post(
                "/internal/command",
                json={
                    "source_entity_id": "light.desk",
                    "command": {"action": "on", "brightness_pct": 30},
                },
                headers=SERVICE,
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json() == {"result": "ok"}
    assert home.calls() == [call("turn_on", "light.desk", brightness_pct=30)]


def test_command_server_reports_refused_and_unreachable_home_assistant() -> None:
    body = {"source_entity_id": "light.desk", "command": {"action": "off"}}
    home = FakeHomeAssistant(success=False)
    with running(home) as origin:
        app = create_app(SERVICE_TOKEN, HomeAssistantLights(origin, HA_TOKEN))
        with TestClient(app) as client:
            refused = client.post("/internal/command", json=body, headers=SERVICE)
    assert (refused.status_code, refused.json()) == (200, {"result": "refused"})

    app = create_app(SERVICE_TOKEN, HomeAssistantLights(closed_origin(), HA_TOKEN))
    with TestClient(app) as client:
        failed = client.post("/internal/command", json=body, headers=SERVICE)
    # Any non-2xx answer other than refused reads as unreachable to the API.
    assert failed.status_code >= 500, failed.text
    assert HA_TOKEN not in failed.text


# The controller's one deadline per command; the API's client waits longer than this.
DEADLINE_SECONDS = 4


def test_slow_home_assistant_within_the_deadline_still_switches_the_light() -> None:
    # Three replies of one second each: three seconds in all.
    home = FakeHomeAssistant(delay=1.0)
    with running(home) as origin:
        result = HomeAssistantLights(origin, HA_TOKEN).send(
            "light.desk", light_command({"action": "on"})
        )
    assert result == "ok"
    assert home.calls() == [call("turn_on", "light.desk")]


def test_home_assistant_slower_than_the_deadline_is_unreachable_within_it() -> None:
    # Each reply is well inside any per-step timeout, but all three take 4.5 s.
    home = FakeHomeAssistant(delay=1.5)
    with running(home) as origin:
        started = time.monotonic()
        with pytest.raises(ControllerUnreachable):
            HomeAssistantLights(origin, HA_TOKEN).send(
                "light.desk", light_command({"action": "on"})
            )
        elapsed = time.monotonic() - started
    assert elapsed < DEADLINE_SECONDS + 0.5, f"gave up after {elapsed:.1f}s"


def test_home_assistant_backend_built_from_the_environment_switches_the_light(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "control-token"
    token_file.write_text(f"{HA_TOKEN}\n")
    home = FakeHomeAssistant()
    with running(home) as origin:
        monkeypatch.setenv("HOME_KEEPER_CONTROL_BACKEND", "home_assistant")
        monkeypatch.setenv("HOME_KEEPER_HA_URL", origin)
        monkeypatch.setenv("HOME_KEEPER_HA_CONTROL_TOKEN_FILE", str(token_file))
        # As the controller's entry point does: the backend named by the environment.
        lights = backend(os.environ["HOME_KEEPER_CONTROL_BACKEND"], SERVICE_TOKEN)
        with TestClient(create_app(SERVICE_TOKEN, lights)) as client:
            response = client.post(
                "/internal/command",
                json={"source_entity_id": "light.desk", "command": {"action": "off"}},
                headers=SERVICE,
            )
    assert (response.status_code, response.json()) == (200, {"result": "ok"})
    assert home.calls() == [call("turn_off", "light.desk")]


def test_unknown_controller_backend_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="HOME_KEEPER_CONTROL_BACKEND"):
        backend("passthrough", SERVICE_TOKEN)


class ApiBridge:
    """The controller's HTTP client to the API, carried by an in-process test client."""

    def __init__(self, api: TestClient) -> None:
        self.api = api

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.api.post(path, headers=SERVICE, **kwargs)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.api.get(path, headers=SERVICE, **kwargs)

    def close(self) -> None:
        pass


@pytest.mark.parametrize("accepted", [True, False], ids=["valid-token", "revoked-token"])
def test_controller_reports_control_only_while_home_assistant_accepts_its_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    (tmp_path / "service-token").write_text(SERVICE_TOKEN)
    (tmp_path / "control-token").write_text(HA_TOKEN if accepted else "revoked-control-token")
    home = FakeHomeAssistant()
    seen: dict[str, Any] = {}
    # Configured like the real API; this test sends commands to the controller directly.
    api_app = create_api(
        tmp_path / "api.sqlite",
        SERVICE_TOKEN,
        controller=HttpController("http://controller:8080", SERVICE_TOKEN),
    )
    with running(home) as origin, TestClient(api_app) as api:
        monkeypatch.setenv("HOME_KEEPER_TOKEN_FILE", str(tmp_path / "service-token"))
        monkeypatch.setenv("HOME_KEEPER_CONTROL_BACKEND", "home_assistant")
        monkeypatch.setenv("HOME_KEEPER_HA_URL", origin)
        monkeypatch.setenv("HOME_KEEPER_HA_CONTROL_TOKEN_FILE", str(tmp_path / "control-token"))

        def api_client(**_kwargs: object) -> ApiBridge:
            return ApiBridge(api)

        monkeypatch.setattr(server.httpx, "Client", api_client)

        def serve(app: Any, **_kwargs: object) -> None:
            # Runs where the controller would start serving: its heartbeat is already running.
            states: list[str] = []
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                states.append(api.get("/api/control").json()["state"])
                if accepted and states[-1] == "on":
                    break
                time.sleep(0.05)
            with TestClient(app) as controller:
                seen["command"] = controller.post(
                    "/internal/command",
                    json={"source_entity_id": "light.desk", "command": {"action": "on"}},
                    headers=SERVICE,
                )
            seen["states"] = states
            seen["after"] = api.get("/api/control").json()["state"]

        monkeypatch.setattr(server.uvicorn, "run", serve)
        server.main()
        for thread in threads():
            if thread.name == "control-heartbeat":
                thread.join(timeout=10)
    response = seen["command"]
    if accepted:
        assert seen["states"][-1] == "on"
        assert (response.status_code, response.json()) == (200, {"result": "ok"})
        assert seen["after"] == "on"
        assert home.calls() == [call("turn_on", "light.desk")]
    else:
        assert set(seen["states"]) == {"off"}, "control reported on with a rejected token"
        assert response.status_code == 502, response.text
        assert seen["after"] == "off"
        assert all(frame.get("type") == "auth" for frames in home.connections for frame in frames)
