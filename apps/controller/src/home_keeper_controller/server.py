"""The controller's HTTP entry point and its heartbeat to the API.

Run with `python -m home_keeper_controller.server`.
"""

import logging
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
from home_keeper_controller.lights import Devices, valid_token
from home_keeper_controller.settings import ControllerSettings
from home_keeper_light_command import ControllerUnreachable, ForwardedCommand, ForwardResult
from home_keeper_light_command.client import CommandClient
from home_keeper_settings import load

HEARTBEAT_SECONDS = 5
MAX_BODY_BYTES = 64 * 1024
# The demo reader's command endpoint on the internal control network.
DEMO_READER_URL = "http://reader:8080"


def create_app(service_token: str, devices: Devices) -> FastAPI:
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
            result = devices.send(forwarded)
        except ControllerUnreachable:
            raise HTTPException(502, "Device backend unreachable") from None
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


def backend(settings: ControllerSettings, service_token: str) -> Devices:
    if settings.control_backend == "demo":
        return CommandClient(DEMO_READER_URL, service_token, COMMAND_DEADLINE_SECONDS)
    token_file = settings.ha_control_token_file
    return HomeAssistantLights(
        settings.ha_url, lambda: read_secret(token_file, "Home Assistant control token")
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Library debug output can include request headers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = load(ControllerSettings)
        # The API creates the service credential at its first start.
        while not settings.token_file.exists():
            time.sleep(1)
        service_token = read_secret(settings.token_file, "Service credential")
        devices = backend(settings, service_token)
    except ValueError as error:
        # Messages name the variable or file and the reason, never a value or credential.
        logging.error("Controller configuration invalid: %s", error)
        raise SystemExit(1) from None
    api = httpx.Client(
        # The API has one fixed host on the control network, not a configurable passthrough.
        base_url=f"http://api:{settings.port}",
        headers={"Authorization": f"Bearer {service_token}"},
        timeout=5,
        trust_env=False,
        follow_redirects=False,
    )
    # Only Home Assistant can reject the backend's credential; the demo is always usable.
    usable = devices.usable if isinstance(devices, HomeAssistantLights) else lambda: True
    stop = Event()
    Thread(
        target=heartbeat, args=(api, stop, usable), name="control-heartbeat", daemon=True
    ).start()
    try:
        uvicorn.run(
            create_app(service_token, devices),
            host=settings.controller_bind,
            port=settings.controller_port,
            access_log=False,
        )
    finally:
        stop.set()
        api.close()


if __name__ == "__main__":
    main()
