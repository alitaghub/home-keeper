"""Independent live transport boundary probes."""

import json
import logging

import httpx
import pytest
from test_home_assistant import Socket, row

from home_observer.home_assistant import MAX_PAYLOAD, ConnectorError, HomeAssistantConnector


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("review-ha-secret")
    socket = Socket([row()])
    monkeypatch.setattr("home_observer.home_assistant.connect", lambda *a, **kw: socket)
    client = HomeAssistantConnector("http://ha.example:8123", secret)
    client.discover()
    client.approve({"ha:registry1": 1})
    yield client
    client.close()


def transport(client, handler):
    client._http.close()
    client._http = httpx.Client(base_url=client.origin, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("kind", ["oversized", "wrong_identity", "unsupported_state", "redirect"])
def test_invalid_selected_response_is_safe(adapter, kind, caplog):
    payload = {"entity_id": "light.bulb", "state": "on", "attributes": {}}
    if kind == "oversized":
        payload["untrusted"] = "review-ha-secret" + "x" * MAX_PAYLOAD
    if kind == "wrong_identity":
        payload["entity_id"] = "light.unapproved"
    if kind == "unsupported_state":
        payload["state"] = "review-ha-secret"
    calls = []

    def response(request):
        calls.append(request.url)
        return httpx.Response(
            302 if kind == "redirect" else 200,
            json=payload,
            headers={"Location": "http://admin.example/write"},
        )

    transport(adapter, response)
    with caplog.at_level(logging.DEBUG), pytest.raises(ConnectorError) as error:
        adapter.read_selected("ha:registry1", 1)
    assert len(calls) == 1
    assert calls[0].path == "/api/states/light.bulb"
    assert "review-ha-secret" not in str(error.value)
    assert "review-ha-secret" not in caplog.text


def test_timeout_and_discovery_revoke_do_not_leak(adapter, monkeypatch, caplog):
    def timeout(request):
        raise httpx.ReadTimeout("review-ha-secret", request=request)

    transport(adapter, timeout)
    with caplog.at_level(logging.DEBUG), pytest.raises(ConnectorError) as error:
        adapter.read_selected("ha:registry1", 1)
    assert error.value.code == "timeout"
    assert "review-ha-secret" not in str(error.value)
    assert "review-ha-secret" not in caplog.text
    socket = Socket([row("replacement")])
    monkeypatch.setattr("home_observer.home_assistant.connect", lambda *a, **kw: socket)
    adapter.discover()
    with pytest.raises(ConnectorError):
        adapter.approve({"ha:registry1": 1})
    with pytest.raises(ConnectorError):
        adapter.read_selected("ha:registry1", 1)


def test_registry_payload_limit_before_projection(adapter, monkeypatch):
    socket = Socket([row()])
    socket.messages[2]["result"][0]["unused"] = "x" * MAX_PAYLOAD
    monkeypatch.setattr("home_observer.home_assistant.connect", lambda *a, **kw: socket)
    with pytest.raises(ConnectorError) as error:
        adapter.discover()
    assert error.value.code == "invalid_data"
    assert len(socket.sent) == 2
    assert json.loads(json.dumps(socket.sent[1]))["type"] == "config/entity_registry/list"


def test_initial_missing_baseline_stays_selected_then_recovers(adapter, tmp_path):
    from fastapi.testclient import TestClient

    from home_observer.api import create_app
    from home_observer.live import reconcile

    status = [404]
    transport(
        adapter,
        lambda request: httpx.Response(
            status[0], json={"entity_id": "light.bulb", "state": "off", "attributes": {}}
        ),
    )
    # The adapter keeps using the validated registry fixture on every reconciliation.
    from test_home_assistant import Socket

    import home_observer.home_assistant as ha

    original_connect = ha.connect
    ha.connect = lambda *args, **kwargs: Socket([row()])
    try:
        with TestClient(
            create_app(tmp_path / "live.sqlite", "review-internal", source_id="home_assistant"),
            headers={"Authorization": "Bearer review-internal"},
        ) as api:
            reconcile(api, adapter)
            api.put(
                "/api/selection/ha:registry1",
                json={"selected": True},
                headers={"X-Home-Observer": "1"},
            ).raise_for_status()
            reconcile(api, adapter)
            assert api.get("/api/selection").json()["entity_ids"] == ["ha:registry1"]
            assert api.get("/api/health").json()["error"] == "missing"
            monitored = api.get("/api/monitored").json()["entities"][0]
            assert monitored["baseline"] is None
            assert monitored["freshness"] == "pending"
            assert api.get("/api/baselines?entity_id=ha:registry1").json()["baselines"] == []
            status[0] = 200
            reconcile(api, adapter)
            assert api.get("/api/health").json()["status"] == "healthy"
            assert api.get("/api/monitored").json()["entities"][0]["freshness"] == "fresh"
            assert len(api.get("/api/baselines?entity_id=ha:registry1").json()["baselines"]) == 1
    finally:
        ha.connect = original_connect
