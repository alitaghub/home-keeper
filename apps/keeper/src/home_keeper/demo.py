"""Synthetic connector. No device, vendor, network-administration or model access."""

import logging
import os
import time
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TypedDict

import httpx

from home_keeper.events import change_event
from home_keeper.models import LightState
from home_keeper.runtime import load_token

# The replay phase whose events are delivered twice.
DUPLICATE_PHASE = 3


class DemoEntity(TypedDict):
    id: str
    device_id: str | None
    device_name: str | None
    name: str
    supported: bool
    reason: str | None


ENTITIES: list[DemoEntity] = [
    {
        "id": "light.hue_desk",
        "device_id": "hue-desk",
        "device_name": "Desk lamp",
        "name": "Desk light",
        "supported": True,
        "reason": None,
    },
    {
        "id": "light.hue_reading",
        "device_id": "hue-reading",
        "device_name": "Reading lamp",
        "name": "Reading light <script>alert('untrusted')</script>",
        "supported": True,
        "reason": None,
    },
    {
        "id": "light.hue_room",
        "device_id": "hue-room",
        "device_name": "Living room group",
        "name": "All living room lights",
        "supported": False,
        "reason": "Room aggregate, not an individual bulb",
    },
    {
        "id": "switch.smart_plug",
        "device_id": "smart-plug",
        "device_name": "Smart plug",
        "name": "Plug",
        "supported": False,
        "reason": "Only individual Hue lights supported",
    },
    {
        "id": "sensor.ungrouped",
        "device_id": None,
        "device_name": None,
        "name": "Ungrouped sensor",
        "supported": False,
        "reason": "Sensor monitoring is outside this milestone",
    },
]
CATALOG = {"source_id": "demo", "entities": ENTITIES}


def coverage(
    client: httpx.Client, status: str, reason: str = "connection", revision: int | None = None
) -> None:
    client.post(
        "/internal/coverage",
        json={
            "source_id": "demo",
            "status": status,
            "reason": reason,
            "selection_revision": revision,
            "received_at": datetime.now(UTC).isoformat(),
        },
    ).raise_for_status()


class DemoReplay:
    """Repeatable synthetic transitions. Unselected history is never forwarded."""

    def __init__(self) -> None:
        self.phase = 0
        self.states = {
            entity["id"]: LightState.model_validate(
                {
                    "state": "on",
                    "brightness": 180 if entity["id"] == "light.hue_desk" else 100,
                    "last_changed": "2025-01-01T12:00:00Z",
                    "last_updated": "2025-01-01T12:00:00Z",
                }
            )
            for entity in ENTITIES
            if entity["supported"]
        }

    def advance(self, event_time: datetime) -> dict[str, LightState]:
        sequence: tuple[dict[str, object], ...] = (
            {"state": "off"},
            {"state": "on", "brightness": 100},
            {"state": "on", "brightness": 40},
            {"state": "on", "color_mode": "color_temp", "color_temp_kelvin": 2700},
            {"state": "on", "color_mode": "xy", "xy_color": (0.4, 0.3)},
            {"state": "unavailable"},
            {"state": "on", "brightness": 180, "color_mode": "brightness"},
        )
        value = sequence[self.phase % len(sequence)]
        self.phase += 1
        previous = self.states
        self.states = {
            identity: LightState.model_validate(
                {
                    **value,
                    "last_changed": (
                        event_time if old.state != value["state"] else old.last_changed
                    ),
                    "last_updated": event_time,
                    "last_reported": event_time,
                }
            )
            for identity, old in previous.items()
        }
        return previous


def reconcile(client: httpx.Client, replay: DemoReplay) -> None:
    client.post("/internal/catalog", json=CATALOG).raise_for_status()
    selection = client.get("/internal/selection")
    selection.raise_for_status()
    selected = selection.json()
    approved = [identity for identity in selected["entity_ids"] if identity in replay.states]
    for identity in approved:
        observation = replay.states[identity].model_dump(mode="json", exclude_none=True) | {
            "entity_id": identity,
            "source_entity_id": identity,
            "selection_revision": selected["revision"],
            "source_id": "demo",
            "received_at": datetime.now(UTC).isoformat(),
        }
        response = client.post("/internal/baselines", json=observation)
        if response.status_code != HTTPStatus.CONFLICT:
            response.raise_for_status()
    fired = datetime.now(UTC)
    previous = replay.advance(fired)
    for identity in approved:
        event = change_event(
            source_id="demo",
            entity_id=identity,
            source_entity_id=identity,
            revision=selected["revision"],
            event_time=fired,
            old=previous[identity],
            new=replay.states[identity],
        )
        payload = event.model_dump(mode="json", exclude_none=True)
        # Replay one duplicate to demonstrate idempotent delivery without duplicate evidence.
        for _ in range(2 if replay.phase == DUPLICATE_PHASE else 1):
            response = client.post("/internal/events", json=payload)
            if response.status_code != HTTPStatus.CONFLICT:
                response.raise_for_status()
    client.post(
        "/internal/health",
        json={
            "source_id": "demo",
            "status": "healthy",
            "received_at": datetime.now(UTC).isoformat(),
        },
    ).raise_for_status()
    coverage(client, "recovered", revision=selected["revision"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # The demo has one fixed API host, not a user-supplied HTTP passthrough.
    api_url = f"http://api:{int(os.environ.get('HOME_KEEPER_PORT', '8000'))}"
    token_path = Path(os.environ.get("HOME_KEEPER_TOKEN_FILE", "/secrets/service-token"))
    interval = int(os.environ.get("HOME_KEEPER_RECONCILIATION_SECONDS", "10"))
    if interval < 1:
        raise ValueError("Reconciliation interval must be positive")
    while not token_path.exists():
        time.sleep(1)
    token = load_token(token_path)
    with httpx.Client(
        base_url=api_url, headers={"Authorization": f"Bearer {token}"}, timeout=5, trust_env=False
    ) as client:
        replay = DemoReplay()
        while True:
            try:
                reconcile(client, replay)
            except (httpx.HTTPError, ValueError):
                # Never log request headers, responses or exception strings.
                logging.warning("Demo collection failed; retrying on next reconciliation")
                try:
                    coverage(client, "interrupted", "ingestion")
                    client.post(
                        "/internal/health",
                        json={
                            "source_id": "demo",
                            "status": "failed",
                            "received_at": datetime.now(UTC).isoformat(),
                        },
                    )
                except httpx.HTTPError:
                    pass
            time.sleep(interval)


if __name__ == "__main__":
    main()
