"""Live collector session and process startup against mocked API and connector."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from home_observer import live
from home_observer.home_assistant import SubscriptionKey
from home_observer.live import CollectionSession
from home_observer.models import Catalog, ChangeEvent, Entity, Observation


class IdleSubscription:
    def __init__(self, key: SubscriptionKey) -> None:
        self.key = key

    def receive(self, timeout: float = 1) -> ChangeEvent | None:
        return None

    def close(self) -> None:
        pass


def test_large_selection_finishes_with_progress_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    selected = [f"ha:light{number}" for number in range(32)]
    calls: list[tuple[str, Any]] = []
    key: SubscriptionKey = (("fixture", 1, "light.fixture"),)

    class Connector:
        subscription_key = key

        def discover(self) -> Catalog:
            return Catalog(
                source_id="home_assistant",
                entities=[
                    Entity(id=identity, name=identity, supported=True) for identity in selected
                ],
            )

        def approve(self, selected: dict[str, int]) -> None:
            pass

        def subscribe_selected(self) -> IdleSubscription:
            return IdleSubscription(key)

        def read_selected(self, identity: str, revision: int) -> Observation:
            clock[0] += 1
            return Observation.model_validate(
                {
                    "entity_id": identity,
                    "selection_revision": revision,
                    "source_id": "home_assistant",
                    "state": "on",
                    "availability": "available",
                    "received_at": "2025-01-01T00:00:00Z",
                }
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("home_observer.live.time", SimpleNamespace(monotonic=lambda: clock[0]))

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/selection":
            return httpx.Response(200, json={"revision": 1, "entity_ids": selected})
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    with httpx.Client(base_url="http://api", transport=httpx.MockTransport(api)) as client:
        session = CollectionSession(client, Connector())
        try:
            assert session.reconcile()
            assert len([path for path, _ in calls if path == "/internal/baselines"]) == 32
            assert len([path for path, _ in calls if path == "/internal/health"]) >= 4
            recovered = [v for path, v in calls if path == "/internal/coverage"][-1]
            assert recovered["status"] == "recovered"
            assert recovered["entity_ids"] == selected
        finally:
            session.close()


def test_startup_configuration_failure_posts_safe_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service_token = tmp_path / "service-token"
    service_token.write_text("internal-startup-sentinel")
    ha_token = tmp_path / "ha-token"
    ha_token.write_text("ha-startup-sentinel")
    monkeypatch.setenv("OBSERVER_TOKEN_FILE", str(service_token))
    monkeypatch.setenv("OBSERVER_HA_TOKEN_FILE", str(ha_token))
    monkeypatch.setenv("OBSERVER_HA_URL", "http://user:ha-startup-sentinel@host")
    calls: list[Any] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200)

    api_client = httpx.Client(base_url="http://api:8000", transport=httpx.MockTransport(api))

    def client(**_kwargs: object) -> httpx.Client:
        return api_client

    monkeypatch.setattr(live.httpx, "Client", client)
    with pytest.raises(SystemExit) as error:
        live.main()
    assert error.value.code == 1
    assert calls[0]["error"] == "configuration"
    assert "internal-startup-sentinel" not in caplog.text
    assert "ha-startup-sentinel" not in caplog.text
