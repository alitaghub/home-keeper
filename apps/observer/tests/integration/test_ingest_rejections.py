"""The collector branches on these exact rejections, so status, code and message are pinned."""

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from support.api import AUTH, CATALOG, NOW, Payload, app_for, event, observation, select

LATER = (NOW + timedelta(seconds=1)).isoformat()
EARLIER = (NOW - timedelta(seconds=1)).isoformat()

type Case = tuple[str, list[Payload], Payload, int, object]


def cases(revision: int) -> list[Case]:
    return [
        (
            "/internal/baselines",
            [],
            observation(revision, source_id="home_assistant"),
            409,
            {"code": "source_mismatch", "message": "Collection source mismatch"},
        ),
        (
            "/internal/baselines",
            [],
            observation(revision, received_at=LATER),
            422,
            "Future receipt timestamp",
        ),
        (
            "/internal/baselines",
            [],
            observation(revision + 1),
            409,
            {
                "code": "selection_changed",
                "message": "Selection no longer permits this observation",
            },
        ),
        (
            "/internal/baselines",
            [observation(revision)],
            observation(revision, received_at=EARLIER),
            409,
            {"code": "observation_older", "message": "Observation is older than latest snapshot"},
        ),
        (
            "/internal/events",
            [],
            event(revision, source_id="home_assistant"),
            409,
            {"code": "source_mismatch", "message": "Collection source mismatch"},
        ),
        (
            "/internal/events",
            [],
            event(revision, received_at=LATER, event_time=NOW.isoformat()),
            422,
            "Future event or receipt timestamp",
        ),
        (
            "/internal/events",
            [],
            event(revision, event_time=LATER),
            422,
            "Future event or receipt timestamp",
        ),
        (
            "/internal/events",
            [],
            event(revision + 1),
            409,
            {"code": "selection_changed", "message": "Selection no longer permits this event"},
        ),
        (
            "/internal/events",
            [event(revision)],
            event(revision, new={"state": "on", "brightness": 50}),
            409,
            {"code": "event_identity_conflict", "message": "Event identity conflict"},
        ),
    ]


@pytest.mark.parametrize("index", range(len(cases(1))))
def test_ingest_rejections_keep_their_status_and_code(tmp_path: Path, index: int) -> None:
    with TestClient(app_for(tmp_path / "rejections.sqlite")) as client:
        client.post("/internal/catalog", json=CATALOG, headers=AUTH)
        revision = select(client)
        path, setup, payload, status, detail = cases(revision)[index]
        for earlier in setup:
            assert client.post(path, json=earlier, headers=AUTH).is_success
        response = client.post(path, json=payload, headers=AUTH)
        assert response.status_code == status, response.text
        assert response.json()["detail"] == detail
