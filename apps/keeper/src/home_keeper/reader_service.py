"""The reader: the only keeper process that holds the Home Assistant owner token.

It answers the API's reads over the internal network and keeps no state of its own: no
loop, no database, no service calls and no event subscription. Only the projected catalog
is cached, for up to CATALOG_SECONDS; state and history are read anew on every request.
Run with `python -m home_keeper.reader_service`.
"""

import logging
import secrets
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from threading import Lock, Thread
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, TypeAdapter, ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from home_keeper.demo import ENTITIES, DemoReplay
from home_keeper.home_assistant import VISIBLE_ASCII, ConnectorError, HomeAssistantConnector
from home_keeper.models import (
    CollectorError,
    Entity,
    Identity,
    LightState,
    Reading,
    SwitchState,
)
from home_keeper.reader import (
    HistoryRow,
    LiveState,
    Reader,
    ReaderCatalog,
    ReaderFailure,
    ReaderHealthy,
    ReaderHistory,
    ReaderStates,
    ReaderUnavailable,
)
from home_keeper.settings import ReaderSettings
from home_keeper.tokens import load_token
from home_keeper_light_command import ForwardedCommand, ForwardResult
from home_keeper_settings import SettingsError, load

# How long the projected catalog may be reused. Never applies to state.
CATALOG_SECONDS = 10
# How often the synthetic home moves to its next replay phase.
DEMO_STEP_SECONDS = 10
MAX_BODY_BYTES = 64 * 1024
# A whole selection fits; see Selection.entity_ids.
MAX_STATE_IDS = 500
# The demo starts with this much synthetic history, one replay phase per step.
DEMO_HISTORY = timedelta(hours=24)
DEMO_HISTORY_STEP = timedelta(minutes=10)
# The demo forgets history older than this, so a long run does not grow without bound.
DEMO_HISTORY_KEPT = timedelta(days=2)

# What the reader serves: Home Assistant or the synthetic demo home.
type ReaderBackend = Reader

_reading: TypeAdapter[Reading] = TypeAdapter(Reading)
_collector_error: TypeAdapter[CollectorError] = TypeAdapter(CollectorError)


def _reading_of(state: LightState | SwitchState) -> Reading:
    # Unset light attributes stay unset, so the routes leave them out.
    return _reading.validate_python(state.model_dump(mode="json", exclude_none=True))


def live_state(source_entity_id: str, state: LightState | SwitchState) -> LiveState:
    return LiveState(source_entity_id=source_entity_id, reading=_reading_of(state))


def history_row(source_entity_id: str, state: LightState | SwitchState) -> HistoryRow:
    return HistoryRow(source_entity_id=source_entity_id, reading=_reading_of(state))


def _unavailable(error: ConnectorError) -> ReaderUnavailable:
    try:
        code = _collector_error.validate_python(error.code)
    except ValidationError:
        code = "invalid_data"
    return ReaderUnavailable(code)


class HomeAssistantBackend:
    """Reads Home Assistant through the connector's fixed registry and state reads."""

    def __init__(
        self,
        connect: Callable[[], HomeAssistantConnector],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`connect` builds the connector. It runs again on each read until it succeeds, so
        a token file that appears after start is picked up."""
        self._connect = connect
        self._clock = clock
        self._connector: HomeAssistantConnector | None = None
        # Discovery and the group-detection cache it fills are not safe to run twice at once.
        self._lock = Lock()
        self._catalog: tuple[float, list[Entity], dict[str, str]] | None = None

    def _connection(self) -> HomeAssistantConnector:
        with self._lock:
            if self._connector is None:
                try:
                    self._connector = self._connect()
                except ConnectorError as error:
                    raise _unavailable(error) from None
            return self._connector

    def _discover(self) -> tuple[list[Entity], dict[str, str]]:
        connector = self._connection()
        with self._lock:
            now = self._clock()
            if self._catalog is not None and 0 <= now - self._catalog[0] < CATALOG_SECONDS:
                return self._catalog[1], self._catalog[2]
            try:
                catalog = connector.discover()
            except ConnectorError as error:
                self._catalog = None
                raise _unavailable(error) from None
            self._catalog = (now, catalog.entities, connector.source_entity_ids)
            return catalog.entities, self._catalog[2]

    def health(self) -> None:
        try:
            self._connection().check()
        except ConnectorError as error:
            raise _unavailable(error) from None

    def catalog(self) -> list[Entity]:
        return self._discover()[0]

    def states(self, entity_ids: Sequence[Identity]) -> dict[Identity, LiveState | None]:
        entities, sources = self._discover()
        devices = {entity.id for entity in entities if entity.device_kind is not None}
        # Home Assistant no longer has the others, or they are no light or switch.
        requested = {
            entity_id: source
            for entity_id in entity_ids
            if entity_id in devices and (source := sources.get(entity_id)) is not None
        }
        states: dict[str, LightState | SwitchState | None] = {}
        if requested:
            try:
                states = self._connection().read_states(sorted(set(requested.values())))
            except ConnectorError as error:
                raise _unavailable(error) from None
        return {
            entity_id: live_state(source, state)
            if (source := requested.get(entity_id)) is not None
            and (state := states.get(source)) is not None
            else None
            for entity_id in entity_ids
        }

    def history(
        self, entity_ids: Sequence[Identity], start: datetime, end: datetime
    ) -> dict[Identity, list[HistoryRow]]:
        entities, sources = self._discover()
        devices = {entity.id for entity in entities if entity.device_kind is not None}
        # Only lights and switches Home Assistant still has; every other ID has no history.
        requested = {
            entity_id: sources[entity_id]
            for entity_id in entity_ids
            if entity_id in devices and entity_id in sources
        }
        try:
            states = self._connection().history(sorted(set(requested.values())), start, end)
        except ConnectorError as error:
            raise _unavailable(error) from None
        return {
            entity_id: [history_row(source, state) for state in states.get(source, [])]
            if (source := requested.get(entity_id)) is not None
            else []
            for entity_id in entity_ids
        }


class DemoBackend:
    """The synthetic home. The demo controller's commands switch its devices.

    It starts with DEMO_HISTORY of synthetic history, so History is full from the first
    minute, and records every replay step and command after that, as Home Assistant would.
    """

    def __init__(self, replay: DemoReplay | None = None, now: datetime | None = None) -> None:
        self.replay = replay or DemoReplay()
        end = now or datetime.now(UTC)
        moment = end - DEMO_HISTORY
        with self.replay.lock:
            self.replay.states = {
                entity_id: state.model_copy(
                    update={"last_changed": moment, "last_updated": moment, "last_reported": moment}
                )
                for entity_id, state in self.replay.states.items()
            }
            # Each device's states, oldest first.
            self.recorded: dict[str, list[LightState | SwitchState]] = {
                entity_id: [state] for entity_id, state in self.replay.states.items()
            }
            while (moment := moment + DEMO_HISTORY_STEP) <= end:
                self._advance(moment)

    def health(self) -> None:
        return None

    def catalog(self) -> list[Entity]:
        return [Entity.model_validate(entity) for entity in ENTITIES]

    def states(self, entity_ids: Sequence[Identity]) -> dict[Identity, LiveState | None]:
        with self.replay.lock:
            states = dict(self.replay.states)
        return {
            entity_id: None
            if (state := states.get(entity_id)) is None
            else live_state(entity_id, state)
            for entity_id in entity_ids
        }

    def history(
        self, entity_ids: Sequence[Identity], start: datetime, end: datetime
    ) -> dict[Identity, list[HistoryRow]]:
        """Like Home Assistant: the state at `start`, stamped with `start`, then every state
        recorded after `start` and before `end`."""
        with self.replay.lock:
            history = {
                entity_id: list(self.recorded.get(entity_id, ())) for entity_id in entity_ids
            }
        result: dict[Identity, list[HistoryRow]] = {}
        for entity_id, states in history.items():
            before = [state for state in states if _updated(state) <= start]
            during = [state for state in states if start < _updated(state) < end]
            initial = (
                [before[-1].model_copy(update={"last_changed": start, "last_updated": start})]
                if before
                else []
            )
            result[entity_id] = [history_row(entity_id, state) for state in initial + during]
        return result

    def send(self, forwarded: ForwardedCommand) -> Literal["ok", "refused"]:
        with self.replay.lock:
            switched = self.replay.switch(
                forwarded.source_entity_id, forwarded.command, datetime.now(UTC)
            )
            if switched is not None:
                self._record(forwarded.source_entity_id, switched[1])
        return "refused" if switched is None else "ok"

    def step(self) -> None:
        """Move the synthetic home to its next replay phase."""
        with self.replay.lock:
            self._advance(datetime.now(UTC))

    def _advance(self, moment: datetime) -> None:
        """One replay phase, recorded. The caller holds the replay lock."""
        previous = self.replay.advance(moment)
        for entity_id, state in self.replay.states.items():
            # A device a command holds keeps its state and records nothing.
            if state != previous.get(entity_id):
                self._record(entity_id, state)

    def _record(self, entity_id: str, state: LightState | SwitchState) -> None:
        """Append a state and forget what is older than DEMO_HISTORY_KEPT, keeping the
        newest older state as the one in force at the cutoff. The caller holds the lock."""
        states = self.recorded.setdefault(entity_id, [])
        states.append(state)
        cutoff = _updated(state) - DEMO_HISTORY_KEPT
        drop = 0
        while drop + 1 < len(states) and _updated(states[drop + 1]) <= cutoff:
            drop += 1
        del states[:drop]


def _updated(state: LightState | SwitchState) -> datetime:
    # Every synthetic state carries its times; one without would sort first.
    return state.last_updated or datetime.min.replace(tzinfo=UTC)


def create_reader_app(backend: ReaderBackend, service_token: str) -> FastAPI:
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

    # Every route answers a Home Assistant failure with 503 and a ReaderFailure body.
    @app.exception_handler(ReaderUnavailable)
    async def unavailable(_request: Request, exc: ReaderUnavailable) -> JSONResponse:
        # A backend reports Home Assistant's failures; the reader cannot be unreachable to itself.
        error = "connection" if exc.error == "reader_unreachable" else exc.error
        return JSONResponse(ReaderFailure(error=error).model_dump(), status_code=503)

    @app.get("/internal/health", response_model=ReaderHealthy)
    def health() -> ReaderHealthy:
        backend.health()
        return ReaderHealthy(status="healthy")

    @app.get("/internal/catalog", response_model=ReaderCatalog)
    def catalog() -> ReaderCatalog:
        return ReaderCatalog(entities=backend.catalog())

    # Readings omit unset light attributes; excluding unset fields keeps them absent.
    @app.get(
        "/internal/states",
        response_model=ReaderStates,
        response_model_exclude_unset=True,
    )
    def states(
        entity_id: Annotated[list[Identity] | None, Query(max_length=MAX_STATE_IDS)] = None,
    ) -> ReaderStates:
        return ReaderStates(states=backend.states(entity_id or []))

    # Readings omit unset light attributes; excluding unset fields keeps them absent.
    @app.get(
        "/internal/history",
        response_model=ReaderHistory,
        response_model_exclude_unset=True,
    )
    def history(
        start: AwareDatetime,
        end: AwareDatetime,
        entity_id: Annotated[list[Identity] | None, Query(max_length=MAX_STATE_IDS)] = None,
    ) -> ReaderHistory:
        if start > end:
            raise HTTPException(422, "Invalid request")
        return ReaderHistory(
            history=backend.history(entity_id or [], start.astimezone(UTC), end.astimezone(UTC))
        )

    if isinstance(backend, DemoBackend):
        demo = backend

        # The demo controller's target, on the internal control network.
        @app.post("/internal/command", response_model=ForwardResult)
        def command(forwarded: ForwardedCommand) -> ForwardResult:
            return ForwardResult(result=demo.send(forwarded))

    return app


def _demo_steps(backend: DemoBackend) -> None:
    while True:
        time.sleep(DEMO_STEP_SECONDS)
        backend.step()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Library debug output can include request headers or authentication frames.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = load(ReaderSettings)
    except SettingsError as error:
        logging.error("Reader configuration invalid: %s", error)
        raise SystemExit(1) from None
    while not settings.token_file.exists():
        time.sleep(1)
    try:
        token = load_token(settings.token_file)
        if not token or not token.isascii() or any(ord(c) not in VISIBLE_ASCII for c in token):
            raise ValueError
    except (OSError, ValueError):
        logging.error("Reader internal credential invalid; check service token file")
        raise SystemExit(1) from None
    backend: ReaderBackend
    if settings.reader_backend == "demo":
        demo = DemoBackend()
        Thread(target=_demo_steps, args=(demo,), name="demo-steps", daemon=True).start()
        backend = demo
    else:
        # A configuration error is reported on every read, so it reaches the API's health.
        backend = HomeAssistantBackend(
            lambda: HomeAssistantConnector(
                settings.ha_url, settings.ha_token_file, redaction_values=(token,)
            )
        )
    uvicorn.run(
        create_reader_app(backend, token),
        host=settings.reader_bind,
        port=settings.reader_port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
