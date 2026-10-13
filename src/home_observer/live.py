"""Live collector process: credentials remain here, outside API and dashboard."""

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from home_observer.home_assistant import ConnectorError, HomeAssistantConnector
from home_observer.runtime import load_token


def _health(client: httpx.Client, error: str | None):
    client.post(
        "/internal/health",
        json={
            "source_id": "home_assistant",
            "status": "failed" if error else "healthy",
            "received_at": datetime.now(UTC).isoformat(),
            "error": error,
        },
    ).raise_for_status()


def reconcile(client: httpx.Client, connector: HomeAssistantConnector):
    deadline = time.monotonic() + 30
    error = None
    try:
        catalog = connector.discover()
        client.post("/internal/catalog", json=catalog.model_dump(mode="json")).raise_for_status()
        response = client.get("/internal/selection")
        response.raise_for_status()
        selection = response.json()
        supported = {entity.id for entity in catalog.entities if entity.supported}
        selected = {
            identity: selection["revision"]
            for identity in selection["entity_ids"]
            if identity in supported
        }
        if len(selected) != len(selection["entity_ids"]):
            error = "missing"
        connector.approve(selected)
        for identity, revision in selected.items():
            if time.monotonic() >= deadline:
                error = "timeout"
                break
            try:
                observation = connector.read_selected(identity, revision)
                result = client.post(
                    "/internal/baselines", json=observation.model_dump(mode="json")
                )
                if result.status_code != 409:
                    result.raise_for_status()
            except ConnectorError as failure:
                error = failure.code
    except ConnectorError as failure:
        error = failure.code
    _health(client, error)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Library debug output can include request headers or authentication frames.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    service_token_path = Path(os.environ.get("OBSERVER_TOKEN_FILE", "/secrets/service-token"))
    while not service_token_path.exists():
        time.sleep(1)
    try:
        token = load_token(service_token_path)
        if not token or not token.isascii() or any(not 33 <= ord(c) <= 126 for c in token):
            raise ValueError
    except (OSError, ValueError):
        logging.error("Live collector internal credential invalid; check service token file")
        raise SystemExit(1) from None
    with httpx.Client(
        base_url="http://api:8000",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            interval = int(os.environ.get("OBSERVER_RECONCILIATION_SECONDS", "10"))
            if not 1 <= interval <= 3600:
                raise ValueError
            connector = HomeAssistantConnector(
                os.environ.get("OBSERVER_HA_URL", ""),
                Path(os.environ.get("OBSERVER_HA_TOKEN_FILE", "/run/secrets/ha-token")),
                redaction_values=(token,),
            )
        except (ConnectorError, ValueError):
            logging.error("Home Assistant configuration invalid; check origin and token file")
            try:
                _health(client, "configuration")
            except httpx.HTTPError:
                pass
            raise SystemExit(1) from None
        try:
            while True:
                try:
                    reconcile(client, connector)
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    logging.warning("Live collection failed; retrying on next reconciliation")
                    try:
                        _health(client, "connection")
                    except httpx.HTTPError:
                        pass
                time.sleep(interval)
        finally:
            connector.close()


if __name__ == "__main__":
    main()
