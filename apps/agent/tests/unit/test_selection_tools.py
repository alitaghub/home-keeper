import json

import httpx
import pytest
from agent_support import OBSERVER_URL, FakeObserver, call, snapshot

# One call per write tool: (args, method, path, JSON body or None).
WRITES: dict[str, tuple[dict[str, object], str, str, object]] = {
    "refresh_discovery": ({}, "POST", "/api/discovery/refresh", None),
    "monitor_light": ({"entity_id": "bulb-a"}, "PUT", "/api/selection/bulb-a", {"selected": True}),
    "stop_monitoring": (
        {"entity_id": "bulb-a"},
        "PUT",
        "/api/selection/bulb-a",
        {"selected": False},
    ),
}


@pytest.mark.parametrize("name", sorted(WRITES))
def test_write_tool_sends_one_marked_request_to_its_observer_route(name: str) -> None:
    args, method, path, body = WRITES[name]
    observer = FakeObserver(
        {
            "/api/discovery/refresh": snapshot("discovery_refresh"),
            "/api/selection/bulb-a": snapshot("put_selection"),
        }
    )

    result = call(observer.tools(), name, args)

    assert not result.startswith("Observer error:"), result
    [request] = observer.requests
    assert request.method == method
    assert str(request.url) == f"{OBSERVER_URL}{path}"
    assert request.headers["X-Home-Observer"] == "1"
    if body is not None:
        assert json.loads(request.content) == body


@pytest.mark.parametrize(
    ("name", "fixture", "detail"),
    [
        ("monitor_light", "error_unsupported_entity", "Entity is unsupported"),
        ("monitor_light", "error_unknown_entity", "Unknown entity"),
        ("stop_monitoring", "error_unknown_entity", "Unknown entity"),
    ],
)
def test_rejected_selection_change_relays_the_observer_reason(
    name: str, fixture: str, detail: str
) -> None:
    observer = FakeObserver({"/api/selection/bulb-a": snapshot(fixture)})

    result = call(observer.tools(), name, {"entity_id": "bulb-a"})

    assert result.startswith("Observer error:")
    assert detail in result


@pytest.mark.parametrize("name", sorted(WRITES))
def test_unreachable_observer_becomes_an_error_result_for_writes(name: str) -> None:
    args = WRITES[name][0]
    observer = FakeObserver(failure=httpx.ConnectError("connection refused"))

    assert call(observer.tools(), name, args).startswith("Observer error:")
