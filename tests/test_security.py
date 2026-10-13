import logging

import pytest
from fastapi.testclient import TestClient
from test_baselines import observation, select
from test_discovery import AUTH, BROWSER, CATALOG, NOW, TOKEN, app_for


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-credential"}])
def test_every_internal_boundary_rejects_unauthenticated_access(tmp_path, headers, caplog):
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
            ):
                assert client.post(route, json=payload, headers=headers).status_code in (401, 403)
        assert client.get("/api/catalog").json()["entities"] == []
        assert client.get("/api/selection").json()["entity_ids"] == []
        assert "wrong-credential" not in caplog.text
        assert TOKEN not in caplog.text


def test_cross_site_and_missing_custom_header_cannot_change_selection(tmp_path):
    with TestClient(
        app_for(tmp_path / "origin.sqlite"), base_url="http://127.0.0.1:8000"
    ) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        initial = client.get("/api/selection").json()
        for headers in (
            {},
            {"X-Home-Observer": "wrong"},
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


def test_malformed_untrusted_inputs_do_not_leak_secrets_or_offer_write_tools(tmp_path, caplog):
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
            for path in ("/internal/control", "/api/control", "/api/services/light/turn_on"):
                assert client.post(
                    path, json={"entity_id": "bulb-a"}, headers={**AUTH, **BROWSER}
                ).status_code in (404, 405)
        assert TOKEN not in caplog.text
        for endpoint in ("/api/catalog", "/api/selection", "/api/monitored", "/api/health"):
            assert TOKEN not in client.get(endpoint).text
        assert (
            client.get("/api/baselines", params={"entity_id": "bulb-a"}).json()["baselines"] == []
        )


def test_non_ascii_internal_credentials_are_rejected_without_server_error(tmp_path):
    with TestClient(app_for(tmp_path / "invalid-auth.sqlite")) as client:
        response = client.get("/internal/selection", headers={"Authorization": b"Bearer \xff"})
        assert response.status_code in (401, 403)


def test_clock_rollback_does_not_claim_future_collector_heartbeat_is_healthy(tmp_path):
    from datetime import timedelta

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
    tmp_path,
):
    from datetime import timedelta

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
