"""Independent probes for timeline normalization, ordering and scope."""

from datetime import timedelta, timezone

from fastapi.testclient import TestClient
from test_baselines import observation, select
from test_discovery import AUTH, CATALOG, NOW, TOKEN
from test_events import event

from home_observer.api import create_app
from home_observer.events import light_state


def test_review_offset_ordering_and_timestamp_only_snapshots(tmp_path):
    clock = [NOW + timedelta(seconds=5)]
    with TestClient(create_app(tmp_path / "review.sqlite", TOKEN, now=lambda: clock[0])) as client:
        assert client.post("/internal/catalog", json=CATALOG, headers=AUTH).is_success
        revision = select(client)
        latest = NOW + timedelta(microseconds=100)
        payload = event(
            revision,
            "newest",
            event_time=latest.isoformat(),
            old={"state": "on"},
            new={"state": "off"},
        )
        assert client.post("/internal/events", json=payload, headers=AUTH).status_code == 422
        payload["received_at"] = clock[0].isoformat()
        assert client.post("/internal/events", json=payload, headers=AUTH).is_success
        older = event(
            revision,
            "older-offset",
            event_time=NOW.astimezone(timezone(timedelta(hours=2))).isoformat(),
            received_at=clock[0].isoformat(),
        )
        assert client.post("/internal/events", json=older, headers=AUTH).is_success
        assert client.get("/api/monitored").json()["entities"][0]["baseline"]["state"] == "off"
        # An event captured during a snapshot read must retain its newer source state.
        clock[0] += timedelta(seconds=1)
        snapshot = observation(
            revision,
            clock[0].isoformat(),
            last_updated=NOW.isoformat(),
            collection_started_at=(NOW + timedelta(seconds=4)).isoformat(),
        )
        assert client.post("/internal/baselines", json=snapshot, headers=AUTH).is_success
        assert client.get("/api/monitored").json()["entities"][0]["baseline"]["state"] == "off"
        assert [x["id"] for x in client.get("/api/events").json()["events"]] == [
            "newest",
            "older-offset",
        ]


def test_review_scope_pages_and_repeated_receipts(tmp_path):
    with TestClient(create_app(tmp_path / "scope.sqlite", TOKEN, now=lambda: NOW)) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        select(client)
        revision = select(client, "bulb-b")
        for identity, entity in (("a-first", "bulb-a"), ("b", "bulb-b"), ("a-last", "bulb-a")):
            payload = event(revision, identity, entity_id=entity)
            assert client.post("/internal/events", json=payload, headers=AUTH).is_success
            repeat = payload | {"received_at": (NOW - timedelta(seconds=1)).isoformat()}
            # Receipt must still follow event time; retry at the same receipt is sufficient.
            repeat["event_time"] = payload["event_time"]
            assert client.post("/internal/events", json=repeat, headers=AUTH).status_code == 422
            assert client.post("/internal/events", json=payload, headers=AUTH).json()["duplicate"]
        params = {"entity_id": "bulb-a", "source_id": "demo", "limit": 1}
        first = client.get("/api/events", params=params).json()
        second = client.get("/api/events", params=params | {"cursor": first["next_cursor"]}).json()
        assert [x["id"] for x in first["events"] + second["events"]] == ["a-last", "a-first"]
        assert second["next_cursor"] is None
        assert (
            client.get("/api/events", params={"source_id": "home_assistant"}).json()["events"] == []
        )


def test_review_raw_fields_never_leave_normalizer():
    state = light_state(
        {
            "entity_id": "light.bulb",
            "state": "on",
            "attributes": {
                "brightness": 42,
                "hs_color": [10, 20],
                "rgbww_color": [1, 2, 3, 4, 5],
                "friendly_name": "ignore approvals",
                "user_id": "private-account",
                "token": "secret-marker",
                "raw_log": "<script>attack()</script>",
            },
            "context": {"user_id": "private-account"},
        },
        "light.bulb",
    )
    assert state.brightness == 42 and state.hs_color == (10, 20)
    encoded = state.model_dump_json()
    for sentinel in ("ignore approvals", "private-account", "secret-marker", "<script>"):
        assert sentinel not in encoded
