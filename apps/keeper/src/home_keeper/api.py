import logging
import secrets
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import MAXYEAR, UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import anyio.to_thread
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint

from home_keeper.changes import BEFORE_START, read_cursor, read_page
from home_keeper.commands import Commands, Refusal
from home_keeper.control import Controller
from home_keeper.models import (
    CatalogResponse,
    CommandRecord,
    CommandRefused,
    CommandRequest,
    CommandsPage,
    ControlHeartbeat,
    ControlStatus,
    EventsPage,
    HealthStatus,
    Identity,
    MonitoredEntities,
    ReaderError,
    Selection,
    SelectionChange,
    SwitchableKind,
    Unreachable,
    UsageReport,
    to_utc,
)
from home_keeper.monitoring import Monitoring, SelectionFull, UnknownEntity, UnsupportedEntity
from home_keeper.reader import Reader, ReaderUnavailable
from home_keeper.store import Store
from home_keeper.usage import device_usage

logger = logging.getLogger(__name__)

UTC_ZONE = ZoneInfo("UTC")
# History's window when a request names no start.
HISTORY_WINDOW = timedelta(days=30)
EARLIEST_START = datetime.min.replace(tzinfo=UTC) + BEFORE_START

# Fixed text for each reason Home Assistant cannot be read: never upstream text, URLs or tokens.
UNREACHABLE_MESSAGES: dict[ReaderError, str] = {
    "authentication": "Home Assistant rejected the reader's token.",
    "connection": "Home Assistant does not answer. Check its address and the network.",
    "tls": "The certificate of Home Assistant could not be verified.",
    "timeout": "Home Assistant did not answer in time.",
    "invalid_data": "Home Assistant sent data Home Keeper cannot read.",
    "missing": "Home Assistant no longer has a device Home Keeper asked for.",
    "discovery": "Home Assistant refused to list its devices.",
    "configuration": "The reader's Home Assistant address or token file is invalid.",
    "reader_unreachable": "The reader service does not answer.",
}


def create_app(
    database: str,
    service_token: str,
    *,
    database_password: str | None = None,
    reader: Reader,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    source_id: str = "demo",
    home_assistant_url: str | None = None,
    retention_days: int = 30,
    allowed_hosts: tuple[str, ...] = ("testserver", "127.0.0.1:8000", "localhost:8000"),
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8000", "http://localhost:8000"),
    timezone: ZoneInfo = UTC_ZONE,
    controller: Controller | None = None,
    command_wait_seconds: float = 5.0,
    control_stale_seconds: float = 15.0,
) -> FastAPI:
    if (
        isinstance(retention_days, bool)
        # Runtime guard for configuration parsed outside the type checker's view.
        or not isinstance(retention_days, int)  # pyright: ignore[reportUnnecessaryIsInstance]
        or retention_days < 1
    ):
        raise ValueError("Retention must be a positive number of days")
    if not service_token:
        raise ValueError("Service credential required")
    if source_id not in ("demo", "home_assistant"):
        raise ValueError("Unsupported collection source")
    store = Store(database, password=database_password)
    device_commands = Commands(
        store,
        controller,
        reader,
        now=now,
        retention_days=retention_days,
        wait_seconds=command_wait_seconds,
        stale_seconds=control_stale_seconds,
    )
    monitoring = Monitoring(store, reader)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        # At startup, not in create_app, so the OpenAPI schema builds without a database.
        store.open()
        try:
            with store.transaction() as db:
                existing = db.execute("SELECT value FROM metadata WHERE key='source_id'").fetchone()
                if existing and existing[0] != source_id:
                    raise ValueError("Use a separate database for a different collection source")
                db.execute(
                    "INSERT INTO metadata VALUES('source_id', %s) ON CONFLICT DO NOTHING",
                    (source_id,),
                )
            device_commands.interrupt_pending()
            yield
        finally:
            store.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    next_cleanup = 0.0

    def retention_cutoff() -> str:
        return (now().astimezone(UTC) - timedelta(days=retention_days)).isoformat()

    def cleanup() -> None:
        with store.transaction() as db:
            store.cleanup(db, retention_cutoff())

    def conflict(code: str, message: str) -> HTTPException:
        return HTTPException(409, {"code": code, "message": message})

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=400)
        browser_write = request.url.path.startswith("/api/") and request.method not in (
            "GET",
            "HEAD",
        )
        if browser_write and (
            request.headers.get("x-home-keeper") != "1"
            or request.headers.get("sec-fetch-site") == "cross-site"
            or (
                request.headers.get("origin") is not None
                and request.headers["origin"] not in allowed_origins
            )
        ):
            return JSONResponse({"detail": "Request origin denied"}, status_code=403)
        if request.method in ("POST", "PUT"):
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 1024 * 1024:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
                body.extend(chunk)
            # Starlette's middleware replays the cached body to downstream validation.
            request._body = bytes(body)  # pyright: ignore[reportPrivateUsage] -- no public setter
        nonlocal next_cleanup
        if time.monotonic() >= next_cleanup:
            next_cleanup = time.monotonic() + 60
            # In a worker thread: a cleanup waiting on a lock must not stop every request.
            try:
                await anyio.to_thread.run_sync(cleanup)
            except psycopg.Error:
                logger.warning("Command log cleanup failed; it runs again in a minute")
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def internal_auth(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode("utf-8"), f"Bearer {service_token}".encode()):
            raise HTTPException(401, "Service authentication required")

    # Validation details may contain arbitrary incoming text. Keep responses generic.

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.exception_handler(ReaderUnavailable)
    async def unreachable(_request: Request, exc: ReaderUnavailable) -> JSONResponse:
        body = Unreachable(error=exc.error, message=UNREACHABLE_MESSAGES[exc.error])
        return JSONResponse(body.model_dump(), status_code=503)

    needs_home_assistant: dict[int | str, dict[str, object]] = {503: {"model": Unreachable}}

    @app.get("/api/catalog", response_model=CatalogResponse, responses=needs_home_assistant)
    def catalog():
        return CatalogResponse(
            source_id=source_id,
            home_assistant_url=home_assistant_url,
            entities=reader.catalog(),
        )

    @app.get("/api/selection", response_model=Selection)
    def selection():
        return monitoring.selection()

    @app.put("/api/selection/{entity_id}", response_model=Selection, responses=needs_home_assistant)
    def select(entity_id: str, change: SelectionChange):
        result = monitoring.select(entity_id, selected=change.selected)
        if isinstance(result, UnknownEntity):
            raise HTTPException(404, "Unknown entity")
        if isinstance(result, UnsupportedEntity):
            raise HTTPException(422, "Entity is unsupported")
        if isinstance(result, SelectionFull):
            raise HTTPException(422, "Selection is full")
        return result

    # Readings omit unset light attributes; excluding unset fields keeps them absent.
    @app.get(
        "/api/events",
        response_model=EventsPage,
        response_model_exclude_unset=True,
        responses=needs_home_assistant,
    )
    def events(
        *,
        entity_id: Identity | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=1000)] = None,
    ):
        """Changes Home Assistant recorded for monitored devices in `[start, end)`, newest
        first. A cursor keeps the window of the first page and overrides `start` and `end`."""
        try:
            after = None if cursor is None else read_cursor(cursor)
            if after is not None:
                window_start, window_end = to_utc(after.start), to_utc(after.end)
            else:
                window_end = to_utc(end) if end else now().astimezone(UTC)
                window_start = to_utc(start) if start else window_end - HISTORY_WINDOW
                if window_start > window_end:
                    raise ValueError("Reversed range")
            # History is read from just before the window start, which must stay a valid date.
            if window_start < EARLIEST_START:
                raise ValueError("Start before the calendar")
        # OverflowError: a timestamp near year 1 or 9999 can fall outside the calendar.
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(422, "Invalid event scope") from None
        devices = [
            device
            for device in monitoring.selection().entity_ids
            if entity_id is None or device == entity_id
        ]
        return read_page(
            reader, devices, start=window_start, end=window_end, after=after, limit=limit
        )

    @app.get("/api/usage", response_model=UsageReport, responses=needs_home_assistant)
    def usage(
        *,
        start: datetime,
        end: datetime,
        entity_id: Annotated[list[Identity] | None, Query(max_length=500)] = None,
    ):
        """On-time, switch-ons and brightness of selected lights, plugs and switches in
        `[start, end)`, from Home Assistant's history. Other devices have no entry."""
        # A far-past offset can overflow the conversion to UTC.
        try:
            start = to_utc(start)
            end = to_utc(end)
            # The row before `start` is the state at `start` and the predecessor of a change
            # at exactly `start`.
            history_start = start - BEFORE_START
        except (ValueError, OverflowError):
            raise HTTPException(422, "Invalid usage range") from None
        # Splitting by local day looks one day past `end`, which must stay a valid date.
        if start > end or end - start > timedelta(days=366) or end.year >= MAXYEAR:
            raise HTTPException(422, "Invalid usage range")
        current = now().astimezone(UTC)
        kinds = monitoring.kinds()
        # Unselected IDs are left out rather than reported as missing, and devices with no
        # on or off, such as a thermostat, have no usage.
        switchable = {device for device, kind in kinds.items() if kind != "state"}
        devices = sorted(set(entity_id or switchable) & switchable)
        # Home Assistant has no history after now.
        history_end = min(end, current)
        history = (
            reader.history(devices, history_start, history_end)
            if devices and history_start < history_end
            else {}
        )

        def kind_of(device: str) -> SwitchableKind:
            # A device selected without a kind is still known by its rows.
            if (kind := kinds[device]) is not None and kind != "state":
                return kind
            rows = history.get(device, [])
            return "switch" if any(row.reading.device_kind == "switch" for row in rows) else "light"

        return UsageReport(
            start=start.isoformat(),
            end=end.isoformat(),
            timezone=timezone.key,
            devices=[
                device_usage(
                    device,
                    kind_of(device),
                    start=start,
                    end=end,
                    now=current,
                    timezone=timezone,
                    rows=history.get(device, []),
                )
                for device in devices
            ],
        )

    @app.get("/api/health", response_model=HealthStatus)
    def health():
        checked_at = now().astimezone(UTC).isoformat()
        try:
            reader.health()
        except ReaderUnavailable as error:
            return HealthStatus(
                source_id=source_id, status="unreachable", error=error.error, checked_at=checked_at
            )
        return HealthStatus(
            source_id=source_id, status="healthy", error=None, checked_at=checked_at
        )

    # Readings omit unset light attributes; excluding unset fields keeps them absent.
    @app.get("/api/monitored", response_model=MonitoredEntities, response_model_exclude_unset=True)
    def monitored():
        return monitoring.devices()

    @app.post("/internal/control", dependencies=[Depends(internal_auth)], include_in_schema=False)
    def ingest_control(payload: ControlHeartbeat):
        if not device_commands.heartbeat(payload.received_at):
            raise HTTPException(422, "Future receipt timestamp")
        return {"accepted": True}

    @app.get("/api/control", response_model=ControlStatus)
    def control():
        return device_commands.status()

    @app.post(
        "/api/devices/{entity_id}/command",
        response_model=CommandRecord,
        response_model_exclude_unset=True,
        responses={409: {"model": CommandRefused}},
    )
    async def command_device(entity_id: str, request: CommandRequest):
        # Async, so commands that wait for a send slot or an observation hold no worker.
        result = await device_commands.switch(entity_id, request)
        if isinstance(result, UnknownEntity):
            raise HTTPException(404, "Unknown entity")
        if isinstance(result, UnsupportedEntity):
            raise HTTPException(422, "Entity is unsupported")
        if isinstance(result, Refusal):
            # The body CommandRefused declares for the command route's 409.
            raise conflict(result.code, result.message)
        return result

    @app.get("/api/commands", response_model=CommandsPage, response_model_exclude_unset=True)
    def commands(
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
    ):
        page = device_commands.history(start=start, end=end, limit=limit, cursor=cursor)
        if page is None:
            raise HTTPException(422, "Invalid event scope")
        return page

    return app
