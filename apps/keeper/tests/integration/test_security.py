import logging
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from support.api import (
    AUTH,
    BROWSER,
    CATALOG,
    NOW,
    TOKEN,
    app_for,
    catalog_reader,
    observation,
    select,
)
from support.reader import FakeReader

from home_keeper.api import create_app
from home_keeper_light_command import ForwardedCommand


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-credential"}])
def test_every_internal_boundary_rejects_unauthenticated_access(
    tmp_path: Path, headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(app_for(tmp_path / "auth.sqlite")) as client:
        with caplog.at_level(logging.DEBUG):
            control = {"received_at": NOW.isoformat()}
            response = client.post("/internal/control", json=control, headers=headers)
            assert response.status_code in (401, 403)
        assert client.get("/api/control").json()["state"] == "off"
        assert client.get("/api/selection").json()["entity_ids"] == []
        assert "wrong-credential" not in caplog.text
        assert TOKEN not in caplog.text


def test_cross_site_and_missing_custom_header_cannot_change_selection(tmp_path: Path) -> None:
    with TestClient(
        app_for(tmp_path / "origin.sqlite"), base_url="http://127.0.0.1:8000"
    ) as client:
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
        # The collector's ingest routes check their own copy of the catalog.
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
                "/api/devices/bulb-a/turn_on",
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
        response = client.post(
            "/internal/control",
            json={"received_at": NOW.isoformat()},
            headers={b"Authorization": b"Bearer \xff"},
        )
        assert response.status_code in (401, 403)


class RecordingController:
    """Counts forwarded commands; the boundary tests expect none.

    With a `reader`, the device follows each command, so its outcome shows at once.
    """

    def __init__(self, reader: FakeReader | None = None) -> None:
        self.sent: list[str] = []
        self.reader = reader

    def send(self, forwarded: ForwardedCommand) -> Literal["ok", "refused"]:
        self.sent.append(forwarded.source_entity_id)
        if self.reader is not None:
            self.reader.set("bulb-a", forwarded.source_entity_id, {"state": "on"})
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
        tmp_path / "heartbeat.sqlite",
        TOKEN,
        now=lambda: NOW,
        reader=catalog_reader(),
        controller=RecordingController(),
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
    reader = catalog_reader()
    reader.set("bulb-a", "light.desk", {"state": "off"})
    controller = RecordingController(reader)
    app = create_app(
        tmp_path / "command.sqlite",
        TOKEN,
        now=lambda: NOW,
        reader=reader,
        controller=controller,
        command_wait_seconds=0.1,
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.put(
            "/api/selection/bulb-a",
            json={"selected": True},
            headers={**BROWSER, "Origin": "http://127.0.0.1:8000"},
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
            denied = client.post("/api/devices/bulb-a/command", json=body, headers=headers)
            assert denied.status_code in (400, 403), denied.text
        assert controller.sent == []
        assert client.get("/api/commands").json()["commands"] == []
        allowed = client.post(
            "/api/devices/bulb-a/command",
            json=body,
            headers={**BROWSER, "Origin": "http://127.0.0.1:8000"},
        )
        assert allowed.status_code == 200, allowed.text
        assert controller.sent == ["light.desk"]
