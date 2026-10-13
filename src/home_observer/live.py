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
        subscription = None
        next_pass = 0.0
        try:
            while True:
                try:
                    if time.monotonic() >= next_pass:
                        reconcile(client, connector)
                        next_pass = time.monotonic() + interval
                        key = connector.subscription_key
                        if subscription is not None and subscription.key != key:
                            subscription.close()
                            subscription = None
                        if subscription is None and key:
                            subscription = connector.subscribe_selected()
                    if subscription is not None:
                        event = subscription.receive(
                            min(1, max(0.01, next_pass - time.monotonic()))
                        )
                        if event is not None:
                            response = client.post(
                                "/internal/events",
                                json=event.model_dump(mode="json", exclude_none=True),
                            )
                            if response.status_code != 409:
                                response.raise_for_status()
                    else:
                        time.sleep(min(0.5, max(0.01, next_pass - time.monotonic())))
                except (ConnectorError, httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                    logging.warning("Live collection interrupted; retrying on next reconciliation")
                    if subscription is not None:
                        subscription.close()
                        subscription = None
                    try:
                        _health(
                            client,
                            error.code if isinstance(error, ConnectorError) else "connection",
                        )
                    except httpx.HTTPError:
                        pass
                    # One retry per reconciliation interval, with no tight auth/error loop.
                    next_pass = time.monotonic() + interval
                    time.sleep(min(1, interval))
        finally:
            if subscription is not None:
                subscription.close()
            connector.close()


if __name__ == "__main__":
    main()
