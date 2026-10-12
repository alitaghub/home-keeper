"""Synthetic connector. No device, vendor, network-administration or model access."""

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from home_observer.runtime import load_token

CATALOG = {
    "source_id": "demo",
    "entities": [
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
    ],
}


def reconcile(client: httpx.Client, interval: int):
    client.post("/internal/catalog", json=CATALOG).raise_for_status()
    selection = client.get("/internal/selection")
    selection.raise_for_status()
    selected = selection.json()
    for entity_id in selected["entity_ids"]:
        timestamp = datetime.now(UTC).isoformat()
        observation = {
            "entity_id": entity_id,
            "selection_revision": selected["revision"],
            "source_id": "demo",
            "state": "on",
            "availability": "available",
            "received_at": timestamp,
            "last_changed": "2025-01-01T12:00:00Z",
            "last_updated": "2025-01-01T12:00:00Z",
            "last_reported": "2025-01-01T12:00:00Z",
            "brightness": 180 if entity_id == "light.hue_desk" else 100,
        }
        response = client.post("/internal/baselines", json=observation)
        if response.status_code != 409:
            response.raise_for_status()
    client.post(
        "/internal/health",
        json={
            "source_id": "demo",
            "status": "healthy",
            "received_at": datetime.now(UTC).isoformat(),
        },
    ).raise_for_status()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # The demo has one fixed API destination, not a user-supplied HTTP passthrough.
    api_url = "http://api:8000"
    token_path = Path(os.environ.get("OBSERVER_TOKEN_FILE", "/secrets/service-token"))
    interval = int(os.environ.get("OBSERVER_RECONCILIATION_SECONDS", "10"))
    if interval < 1:
        raise ValueError("Reconciliation interval must be positive")
    while not token_path.exists():
        time.sleep(1)
    token = load_token(token_path)
    with httpx.Client(
        base_url=api_url, headers={"Authorization": f"Bearer {token}"}, timeout=5, trust_env=False
    ) as client:
        while True:
            try:
                reconcile(client, interval)
            except (httpx.HTTPError, ValueError):
                # Never log request headers, responses or exception strings.
                logging.warning("Demo collection failed; retrying on next reconciliation")
                try:
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
