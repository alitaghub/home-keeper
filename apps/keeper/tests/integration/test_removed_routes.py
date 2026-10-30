"""The collector's ingest routes and the routes that served its copied data are gone.

Each route is called as its client called it, with valid credentials, so a 404 means the
route no longer exists rather than a refused request. The ingest routes were never in the
OpenAPI document, so the contract snapshots cannot show their removal.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from support.api import AUTH, BROWSER, NOW, app_for

REMOVED = [
    ("GET", "/api/baselines?entity_id=bulb-a", BROWSER),
    ("GET", "/api/coverage", BROWSER),
    ("POST", "/api/discovery/refresh", BROWSER),
    ("POST", "/internal/catalog", AUTH),
    ("POST", "/internal/baselines", AUTH),
    ("POST", "/internal/events", AUTH),
    ("POST", "/internal/health", AUTH),
    ("POST", "/internal/coverage", AUTH),
    ("GET", "/internal/selection", AUTH),
]


@pytest.mark.parametrize(
    ("method", "path", "headers"), [pytest.param(*route, id=route[1]) for route in REMOVED]
)
def test_a_removed_route_is_not_found_with_valid_credentials(
    tmp_path: Path, method: str, path: str, headers: dict[str, str]
) -> None:
    with TestClient(app_for(tmp_path / "home.sqlite")) as client:
        response = (
            client.post(path, json={}, headers=headers)
            if method == "POST"
            else client.get(path, headers=headers)
        )
    assert response.status_code == 404, response.text


def test_the_controller_heartbeat_route_still_accepts_a_heartbeat(tmp_path: Path) -> None:
    with TestClient(app_for(tmp_path / "home.sqlite")) as client:
        response = client.post(
            "/internal/control", json={"received_at": NOW.isoformat()}, headers=AUTH
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": True}
