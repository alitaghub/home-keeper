"""The controller's HTTP entry point and its heartbeat to the API.

Run with `python -m home_keeper_controller.server`.
"""

import logging
import os
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint

from home_keeper_controller.home_assistant import COMMAND_DEADLINE_SECONDS, HomeAssistantLights
from home_keeper_controller.lights import Lights, valid_token
from home_keeper_light_command import ControllerUnreachable, ForwardedCommand, ForwardResult
from home_keeper_light_command.client import CommandClient

HEARTBEAT_SECONDS = 5
MAX_BODY_BYTES = 64 * 1024
# The demo collector's command endpoint on the internal control network.
DEMO_COLLECTOR_URL = "http://collector:8080"


def create_app(service_token: str, lights: Lights) -> FastAPI:
    if not service_token:
        raise ValueError("Service credential required")
    expected = f"Bearer {service_token}".encode()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def service_boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Every request must carry the shared service credential; check it before the body.
        supplied = request.headers.get("authorization", "").encode("utf-8", "replace")
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse({"detail": "Service authentication required"}, status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
            body.extend(chunk)
        # Starlette's middleware replays the cached body to downstream validation.
        request._body = bytes(body)  # pyright: ignore[reportPrivateUsage] -- no public setter
        return await call_next(request)

    # Validation details may echo the request. Keep responses generic.
    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.post("/internal/command", response_model=ForwardResult)
    def command(forwarded: ForwardedCommand) -> ForwardResult:
        try:
            result = lights.send(forwarded.source_entity_id, forwarded.command)
        except ControllerUnreachable:
            raise HTTPException(502, "Light backend unreachable") from None
        return ForwardResult(result=result)

    return app


def heartbeat(
    api: httpx.Client,
    stop: Event,
    usable: Callable[[], bool] = lambda: True,
    interval: float = HEARTBEAT_SECONDS,
) -> None:
    """Tell the API that control is available while `usable()` holds, until `stop` is set."""
    failing = False
    while not stop.is_set():
        if not usable():
            # No heartbeat, so the API reports control off once the last one goes stale.
            stop.wait(interval)
            continue
        try:
            api.post(
                "/internal/control", json={"received_at": datetime.now(UTC).isoformat()}
            ).raise_for_status()
        except httpx.HTTPError:
            # Log the change only, and never the response or exception text.
            if not failing:
                logging.warning("Controller heartbeat to the API failed; retrying")
            failing = True
        else:
            if failing:
                logging.info("Controller heartbeat to the API recovered")
            failing = False
        stop.wait(interval)


def read_secret(path: Path, description: str) -> str:
    try:
        token = path.read_text().strip()
    except OSError:
        raise ValueError(f"{description} file is unreadable") from None
    if not valid_token(token):
        raise ValueError(f"{description} file is invalid")
    return token


def backend(name: str, service_token: str) -> Lights:
    if name == "home_assistant":
        return HomeAssistantLights(
            os.environ.get("HOME_KEEPER_HA_URL", ""),
            read_secret(
                Path(
                    os.environ.get("HOME_KEEPER_HA_CONTROL_TOKEN_FILE", "/ha-control-token/token")
                ),
                "Home Assistant control token",
            ),
        )
    if name == "demo":
        return CommandClient(DEMO_COLLECTOR_URL, service_token, COMMAND_DEADLINE_SECONDS)
    raise ValueError("HOME_KEEPER_CONTROL_BACKEND must be home_assistant or demo")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Library debug output can include request headers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    service_token_path = Path(os.environ.get("HOME_KEEPER_TOKEN_FILE", "/secrets/service-token"))
    # The API creates the service credential at its first start.
    while not service_token_path.exists():
        time.sleep(1)
    try:
        service_token = read_secret(service_token_path, "Service credential")
        lights = backend(
            os.environ.get("HOME_KEEPER_CONTROL_BACKEND", "home_assistant"), service_token
        )
        port = int(os.environ.get("HOME_KEEPER_CONTROLLER_PORT", "8080"))
        api_port = int(os.environ.get("HOME_KEEPER_PORT", "8000"))
    except ValueError as error:
        # Messages are fixed strings; they never contain a credential.
        logging.error("Controller configuration invalid: %s", error)
        raise SystemExit(1) from None
    api = httpx.Client(
        # The API has one fixed host on the control network, not a configurable passthrough.
        base_url=f"http://api:{api_port}",
        headers={"Authorization": f"Bearer {service_token}"},
        timeout=5,
        trust_env=False,
        follow_redirects=False,
    )
    # Only Home Assistant can reject the backend's credential; the demo is always usable.
    usable = lights.usable if isinstance(lights, HomeAssistantLights) else lambda: True
    stop = Event()
    Thread(
        target=heartbeat, args=(api, stop, usable), name="control-heartbeat", daemon=True
    ).start()
    try:
        uvicorn.run(
            create_app(service_token, lights),
            # Inside the container only; Compose publishes no port for the controller.
            host=os.environ.get("HOME_KEEPER_CONTROLLER_BIND", "0.0.0.0"),  # noqa: S104 -- internal network only
            port=port,
            access_log=False,
        )
    finally:
        stop.set()
        api.close()


if __name__ == "__main__":
    main()
