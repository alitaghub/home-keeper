import logging
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from support.api import AUTH, BROWSER, CATALOG, NOW, TOKEN, app_for, observation, select

from home_keeper.api import create_app


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-credential"}])
def test_every_internal_boundary_rejects_unauthenticated_access(
    tmp_path: Path, headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(app_for(tmp_path / "auth.sqlite")) as client:
        with caplog.at_level(logging.DEBUG):
            assert client.get("/internal/selection", headers=headers).status_code in (401, 403)
            for route, payload in (
                ("/internal/catalog", CATALOG),
                ("/internal/baselines", observation(0)),
                (
                    "/internal/health",
                    {"source_id": "demo", "status": "healthy", "received_at": NOW.isoformat()},
                ),
                ("/internal/control", {"received_at": NOW.isoformat()}),
            ):
                assert client.post(route, json=payload, headers=headers).status_code in (401, 403)
        assert client.get("/api/catalog").json()["entities"] == []
        assert client.get("/api/selection").json()["entity_ids"] == []
        assert "wrong-credential" not in caplog.text
        assert TOKEN not in caplog.text


def test_cross_site_and_missing_custom_header_cannot_change_selection(tmp_path: Path) -> None:
    with TestClient(
        app_for(tmp_path / "origin.sqlite"), base_url="http://127.0.0.1:8000"
    ) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        initial = client.get("/api/selection").json()
        for headers in (
            {},
            {"X-Home-Keeper": "wrong"},
            {**BROWSER, "Origin": "https://evil.example"},
            {**BROWSER, "Sec-Fetch-Site": "cross-site"},
            {**BROWSER, "Host": "evil.example"},
            {**BROWSER, "Origin": "null"},
        ):
            denied = client.put("/api/selection/bulb-a", json={"selected": True}, headers=headers)
            assert denied.is_client_error, denied.text
            assert client.get("/api/selection").json() == initial
        assert client.put(
            "/api/selection/bulb-a",
            json={"selected": True},
            headers={**BROWSER, "Origin": "http://127.0.0.1:8000"},
        ).is_success


def test_malformed_untrusted_inputs_do_not_leak_secrets_or_offer_write_tools(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(app_for(tmp_path / "input.sqlite")) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        assert "<script>" in client.get("/api/catalog").text
        revision = select(client)
        with caplog.at_level(logging.DEBUG):
            rejected = client.post(
                "/internal/baselines", headers=AUTH, json=observation(revision, received_at=TOKEN)
            )
            assert rejected.is_client_error
            assert TOKEN not in rejected.text
            # Unexpected credentials and raw vendor attributes must not be echoed in errors.
            extra = client.post(
                "/internal/baselines", headers=AUTH, json=observation(revision, password=TOKEN)
            )
            assert extra.is_client_error
            assert TOKEN not in extra.text
            # No generic service passthrough exists beside the fixed light command.
            for path in (
                "/api/control",
                "/api/services/light/turn_on",
                "/api/services/light/turn_off",
                "/api/services/switch/turn_on",
                "/internal/services/light/turn_on",
                "/internal/command",
                "/api/lights/bulb-a/turn_on",
            ):
                assert client.post(
                    path, json={"entity_id": "bulb-a"}, headers={**AUTH, **BROWSER}
                ).status_code in (404, 405)
        assert TOKEN not in caplog.text
        for endpoint in ("/api/catalog", "/api/selection", "/api/monitored", "/api/health"):
            assert TOKEN not in client.get(endpoint).text
        assert (
            client.get("/api/baselines", params={"entity_id": "bulb-a"}).json()["baselines"] == []
        )


def test_non_ascii_internal_credentials_are_rejected_without_server_error(tmp_path: Path) -> None:
    with TestClient(app_for(tmp_path / "invalid-auth.sqlite")) as client:
        response = client.get("/internal/selection", headers={b"Authorization": b"Bearer \xff"})
        assert response.status_code in (401, 403)


def test_clock_rollback_does_not_claim_future_collector_heartbeat_is_healthy(
    tmp_path: Path,
) -> None:
    clock = [NOW]
    with TestClient(app_for(tmp_path / "clock.sqlite", now=lambda: clock[0])) as client:
        assert client.post(
            "/internal/health",
            headers=AUTH,
            json={"source_id": "demo", "status": "healthy", "received_at": NOW.isoformat()},
        ).is_success
        clock[0] = NOW - timedelta(hours=1)
        assert client.get("/api/health").json()["status"] in ("stale", "unknown", "pending")
        assert client.post(
            "/internal/health",
            headers=AUTH,
            json={"source_id": "demo", "status": "healthy", "received_at": clock[0].isoformat()},
        ).is_success
        assert client.get("/api/health").json()["status"] == "healthy"


def test_delayed_heartbeat_cannot_clear_newer_collection_failure_but_new_heartbeat_recovers(
    tmp_path: Path,
) -> None:
    clock = [NOW + timedelta(seconds=10)]
    with TestClient(app_for(tmp_path / "health-order.sqlite", now=lambda: clock[0])) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        revision = select(client)
        assert client.post(
            "/internal/baselines", json=observation(revision, clock[0].isoformat()), headers=AUTH
        ).is_success
        assert client.post(
            "/internal/health",
            headers=AUTH,
            json={"source_id": "demo", "status": "failed", "received_at": clock[0].isoformat()},
        ).is_success
        client.post(
            "/internal/health",
            headers=AUTH,
            json={
                "source_id": "demo",
                "status": "healthy",
                "received_at": (NOW + timedelta(seconds=5)).isoformat(),
            },
        )
        assert client.get("/api/health").json()["status"] == "failed"
        assert client.get("/api/monitored").json()["entities"][0]["freshness"] == "stale"
        clock[0] += timedelta(seconds=1)
        assert client.post(
            "/internal/health",
            headers=AUTH,
            json={"source_id": "demo", "status": "healthy", "received_at": clock[0].isoformat()},
        ).is_success
        assert client.get("/api/health").json()["status"] == "healthy"
        # Heartbeat recovery alone is not proof that missing observations were saved.
        assert client.get("/api/monitored").json()["entities"][0]["freshness"] == "stale"
        client.post(
            "/internal/baselines", json=observation(revision, clock[0].isoformat()), headers=AUTH
        ).raise_for_status()
        client.post(
            "/internal/coverage",
            json={
                "source_id": "demo",
                "status": "recovered",
                "selection_revision": revision,
                "received_at": clock[0].isoformat(),
            },
            headers=AUTH,
        ).raise_for_status()
        assert client.get("/api/monitored").json()["entities"][0]["freshness"] == "fresh"


class RecordingController:
    """Counts forwarded commands; the boundary tests expect none."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, source_entity_id: str, command: object) -> Literal["ok", "refused"]:
        self.sent.append(source_entity_id)
        return "ok"


def heartbeat_state(client: TestClient) -> object:
    return client.get("/api/control").json()["state"]


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong-credential"}, BROWSER, {"Authorization": TOKEN}]
)
def test_controller_heartbeat_needs_the_service_credential(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    app = create_app(
        tmp_path / "heartbeat.sqlite", TOKEN, now=lambda: NOW, controller=RecordingController()
    )
    with TestClient(app) as client:
        denied = client.post(
            "/internal/control", json={"received_at": NOW.isoformat()}, headers=headers
        )
        assert denied.status_code == 401, denied.text
        assert heartbeat_state(client) == "off"
        assert client.post(
            "/internal/control", json={"received_at": NOW.isoformat()}, headers=AUTH
        ).json() == {"accepted": True}
        assert heartbeat_state(client) == "on"


def test_light_command_needs_the_mutation_header_and_a_same_site_origin(tmp_path: Path) -> None:
    controller = RecordingController()
    app = create_app(
        tmp_path / "command.sqlite",
        TOKEN,
        now=lambda: NOW,
        controller=controller,
        command_wait_seconds=0.1,
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH).raise_for_status()
        revision = select(client)
        client.post(
            "/internal/baselines",
            json=observation(revision, state="off", source_entity_id="light.desk"),
            headers=AUTH,
        ).raise_for_status()
        client.post(
            "/internal/control", json={"received_at": NOW.isoformat()}, headers=AUTH
        ).raise_for_status()
        body = {"command": {"action": "on"}, "sender": {"kind": "dashboard"}}
        for headers in (
            {},
            AUTH,
            {"X-Home-Keeper": "wrong"},
            {**BROWSER, "Origin": "https://evil.example"},
            {**BROWSER, "Origin": "null"},
            {**BROWSER, "Sec-Fetch-Site": "cross-site"},
            {**BROWSER, "Host": "evil.example"},
        ):
            denied = client.post("/api/lights/bulb-a/command", json=body, headers=headers)
            assert denied.status_code in (400, 403), denied.text
        assert controller.sent == []
        assert client.get("/api/commands").json()["commands"] == []
        allowed = client.post(
            "/api/lights/bulb-a/command",
            json=body,
            headers={**BROWSER, "Origin": "http://127.0.0.1:8000"},
        )
        assert allowed.status_code == 200, allowed.text
        assert controller.sent == ["light.desk"]
