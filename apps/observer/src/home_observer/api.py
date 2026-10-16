import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import MAXYEAR, UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter
from starlette.middleware.base import RequestResponseEndpoint

from home_observer.models import (
    BaselineRecord,
    BaselinesPage,
    Catalog,
    CatalogResponse,
    ChangeEvent,
    Coverage,
    CoverageGap,
    CoverageReport,
    DiscoveryRefresh,
    Entity,
    EventReading,
    EventRecord,
    EventsPage,
    Freshness,
    Health,
    HealthPending,
    HealthReport,
    HealthStatus,
    Identity,
    LightState,
    MonitoredEntities,
    MonitoredEntity,
    Observation,
    SelectionChange,
    SelectionSnapshot,
    UsageReport,
    to_utc,
)
from home_observer.store import SnapshotRead, Store, current_reading
from home_observer.usage import light_usage

UTC_ZONE = ZoneInfo("UTC")


def create_app(
    db_path: Path,
    service_token: str,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    reconciliation_seconds: int = 60,
    source_id: str = "demo",
    home_assistant_url: str | None = None,
    retention_days: int = 30,
    allowed_hosts: tuple[str, ...] = ("testserver", "127.0.0.1:8000", "localhost:8000"),
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8000", "http://localhost:8000"),
    timezone: ZoneInfo = UTC_ZONE,
) -> FastAPI:
    if (
        isinstance(retention_days, bool)
        # Runtime guard for configuration parsed outside the type checker's view.
        or not isinstance(retention_days, int)  # pyright: ignore[reportUnnecessaryIsInstance]
        or retention_days < 1
    ):
        raise ValueError("Retention must be a positive number of days")
    if reconciliation_seconds < 1 or not service_token:
        raise ValueError("Positive reconciliation interval and service credential required")
    if source_id not in ("demo", "home_assistant"):
        raise ValueError("Unsupported collection source")
    store = Store(Path(db_path))
    with store.transaction() as db:
        existing = db.execute("SELECT value FROM metadata WHERE key='source_id'").fetchone()
        if existing and existing[0] != source_id:
            raise ValueError("Use a separate database for a different collection source")
        # Existing records without source metadata must remain demo-only.
        if (
            not existing
            and source_id != "demo"
            and any(
                db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()  # noqa: S608 -- fixed table names
                for table in ("catalog", "selection", "baselines", "confirmed")
            )
        ):
            raise ValueError("Use an empty database for live mode")
        db.execute("INSERT OR IGNORE INTO metadata VALUES('source_id', ?)", (source_id,))
        db.execute("INSERT OR IGNORE INTO metadata VALUES('refresh_revision', '0')")
        # Migrate retained snapshots to a separate cache before retention removes history.
        for row in db.execute(
            "SELECT b.entity_id, b.data FROM baselines b WHERE b.rowid="
            "(SELECT MAX(newest.rowid) FROM baselines newest WHERE newest.entity_id=b.entity_id) "
            "AND NOT EXISTS (SELECT 1 FROM current_state c WHERE c.entity_id=b.entity_id)"
        ).fetchall():
            if db.execute(
                "SELECT 1 FROM current_state WHERE entity_id=?", (row["entity_id"],)
            ).fetchone():
                continue
            value = BaselineRecord.model_validate_json(row["data"])
            source_time = value.last_updated or value.received_at
            store.update_current(db, row["entity_id"], source_time, value)
        if store.last_confirmed(db):
            store.interrupt(db, source_id, now().astimezone(UTC).isoformat(), "restart")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    next_cleanup = 0.0

    def retention_cutoff() -> str:
        return (now().astimezone(UTC) - timedelta(days=retention_days)).isoformat()

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
            request.headers.get("x-home-observer") != "1"
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
            with store.transaction() as db:
                store.cleanup(db, retention_cutoff())
            next_cleanup = time.monotonic() + 60
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

    @app.get("/api/catalog", response_model=CatalogResponse)
    def catalog():
        with store.transaction() as db:
            refreshed = db.execute("SELECT value FROM metadata WHERE key='catalog_at'").fetchone()
            age = (
                (now() - datetime.fromisoformat(refreshed[0])).total_seconds()
                if refreshed
                else None
            )
            return CatalogResponse(
                source_id=source_id,
                home_assistant_url=home_assistant_url,
                refreshed_at=refreshed[0] if refreshed else None,
                freshness="pending"
                if age is None
                else (
                    "fresh"
                    if 0 <= age <= 2 * reconciliation_seconds
                    and get_health(db).status not in ("failed", "stale")
                    else "stale"
                ),
                entities=[
                    Entity.model_validate_json(row[0])
                    for row in db.execute("SELECT data FROM catalog ORDER BY id")
                ],
            )

    @app.get("/api/selection", response_model=SelectionSnapshot)
    @app.get(
        "/internal/selection",
        response_model=SelectionSnapshot,
        dependencies=[Depends(internal_auth)],
        include_in_schema=False,
    )
    def selection():
        with store.transaction() as db:
            return store.selection(db)

    @app.post("/api/discovery/refresh", response_model=DiscoveryRefresh)
    def refresh_discovery():
        with store.transaction() as db:
            revision = (
                int(
                    db.execute(
                        "SELECT value FROM metadata WHERE key='refresh_revision'"
                    ).fetchone()[0]
                )
                + 1
            )
            db.execute("UPDATE metadata SET value=? WHERE key='refresh_revision'", (str(revision),))
            return DiscoveryRefresh(refresh_revision=revision)

    @app.put("/api/selection/{entity_id}", response_model=SelectionSnapshot)
    def select(entity_id: str, change: SelectionChange):
        with store.transaction() as db:
            entity = store.entity(db, entity_id)
            current = store.selection(db)
            if entity is None and (change.selected or entity_id not in current.entity_ids):
                raise HTTPException(404, "Unknown entity")
            if change.selected and entity is not None and not entity.supported:
                raise HTTPException(422, "Entity is unsupported")
            current = store.selection(db)
            if change.selected != (entity_id in current.entity_ids):
                if change.selected:
                    db.execute("INSERT INTO selection VALUES(?)", (entity_id,))
                    # A fresh selection awaits a new confirmation, even if history exists.
                    db.execute("DELETE FROM confirmed WHERE entity_id=?", (entity_id,))
                    db.execute("DELETE FROM current_state WHERE entity_id=?", (entity_id,))
                else:
                    db.execute("DELETE FROM selection WHERE id=?", (entity_id,))
                db.execute(
                    "UPDATE metadata SET value=? WHERE key='revision'",
                    (str(current.revision + 1),),
                )
            return store.selection(db)

    @app.post("/internal/catalog", dependencies=[Depends(internal_auth)], include_in_schema=False)
    def ingest_catalog(payload: Catalog):
        if payload.source_id != source_id:
            raise conflict("source_mismatch", "Collection source mismatch")
        with store.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('catalog_at', ?)", (now().isoformat(),)
            )
            db.execute("DELETE FROM catalog")
            db.executemany(
                "INSERT INTO catalog VALUES(?, ?)",
                [(e.id, e.model_dump_json()) for e in payload.entities],
            )
            # Discovery describes support; it never changes the user's approval.
        return {"accepted": True}

    @app.post("/internal/baselines", dependencies=[Depends(internal_auth)], include_in_schema=False)
    def ingest_observation(payload: Observation):
        if payload.source_id != source_id:
            raise conflict("source_mismatch", "Collection source mismatch")
        persisted = now().astimezone(UTC)
        if payload.received_at > persisted:
            raise HTTPException(422, "Future receipt timestamp")
        with store.transaction() as db:
            current = store.selection(db)
            entity = store.entity(db, payload.entity_id)
            if (
                payload.selection_revision != current.revision
                or payload.entity_id not in current.entity_ids
                or entity is None
                or not entity.supported
            ):
                raise conflict("selection_changed", "Selection no longer permits this observation")
            confirmed = db.execute(
                "SELECT received_at FROM confirmed WHERE entity_id=?", (payload.entity_id,)
            ).fetchone()
            if confirmed and payload.received_at < datetime.fromisoformat(confirmed[0]):
                raise conflict("observation_older", "Observation is older than latest confirmation")
            data = payload.model_dump(mode="json", exclude={"selection_revision"})
            latest = store.current(db, payload.entity_id)
            # Only an authoritative snapshot lacking a recorded change exposes a gap.
            # A newer buffered event wins over an older reconciliation snapshot.
            if confirmed and latest:
                keys = LightState.model_fields.keys()
                previous_state = LightState.model_validate(
                    latest.model_dump(include=set(keys), exclude_unset=True)
                )
                snapshot_time = payload.last_updated or payload.received_at
                if (
                    LightState.model_validate(
                        {key: getattr(payload, key) for key in keys}
                    ).meaningful()
                    != previous_state.meaningful()
                    and store.snapshot_can_replace(
                        db,
                        payload.entity_id,
                        snapshot_time,
                        SnapshotRead(
                            payload.collection_started_at or payload.received_at,
                            payload.received_at,
                        ),
                    )
                    and not store.open_gaps(db, payload.entity_id)
                ):
                    store.interrupt(
                        db, source_id, persisted.isoformat(), "reconciliation", payload.entity_id
                    )
            gaps = store.open_gaps(db, payload.entity_id)
            data["kind"] = (
                "recovery" if confirmed and gaps else "reconciliation" if confirmed else "baseline"
            )
            if gaps:
                data["coverage_gap_ids"] = [gap.id for gap in gaps]
                data["transition_time"] = "unknown"

            previous = db.execute(
                "SELECT data FROM baselines WHERE entity_id=? ORDER BY rowid DESC LIMIT 1",
                (payload.entity_id,),
            ).fetchone()
            baseline = BaselineRecord.model_validate_json(previous[0]) if previous else None
            comparable = {
                key: value
                for key, value in data.items()
                if key
                not in (
                    "received_at",
                    "kind",
                    "coverage_gap_ids",
                    "transition_time",
                    "collection_started_at",
                )
            }
            old = baseline.model_dump(mode="json", include=set(comparable)) if baseline else None
            new_recovery = bool(gaps) and (
                not baseline or baseline.coverage_gap_ids != data.get("coverage_gap_ids")
            )
            if baseline is None or comparable != old or new_recovery:
                baseline = BaselineRecord.model_validate(
                    data | {"id": str(uuid4()), "persisted_at": persisted.isoformat()}
                )
                db.execute(
                    "INSERT INTO baselines(id,entity_id,data,persisted_at) VALUES(?, ?, ?, ?)",
                    (
                        baseline.id,
                        payload.entity_id,
                        baseline.model_dump_json(exclude_unset=True),
                        persisted.isoformat(),
                    ),
                )
            source_time = payload.last_updated or payload.received_at
            store.update_current(
                db,
                payload.entity_id,
                source_time.isoformat(),
                baseline,
                snapshot=SnapshotRead(
                    payload.collection_started_at or payload.received_at, payload.received_at
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO confirmed VALUES(?, ?, ?)",
                (payload.entity_id, payload.received_at.isoformat(), persisted.isoformat()),
            )
            return {
                "accepted": True,
                "baseline": baseline.model_dump(mode="json", exclude_unset=True),
            }

    # Stored readings omit unset light attributes; excluding unset fields returns them as stored.
    @app.get("/api/baselines", response_model=BaselinesPage, response_model_exclude_unset=True)
    def baselines(
        entity_id: Identity,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[int | None, Query(ge=1)] = None,
    ):
        with store.transaction() as db:
            rows = db.execute(
                # Only fixed SQL fragments are concatenated; values are bound parameters.
                "SELECT rowid,data FROM baselines WHERE entity_id=? AND persisted_at>=? "  # noqa: S608
                + ("AND rowid<? " if cursor is not None else "")
                + "ORDER BY rowid DESC LIMIT ?",
                (
                    entity_id,
                    retention_cutoff(),
                    *((cursor,) if cursor is not None else ()),
                    limit + 1,
                ),
            ).fetchall()
            return BaselinesPage(
                baselines=[BaselineRecord.model_validate_json(row["data"]) for row in rows[:limit]],
                next_cursor=rows[limit - 1]["rowid"] if len(rows) > limit else None,
            )

    @app.post("/internal/events", dependencies=[Depends(internal_auth)], include_in_schema=False)
    def ingest_event(payload: ChangeEvent):
        if payload.source_id != source_id:
            raise conflict("source_mismatch", "Collection source mismatch")
        persisted = now().astimezone(UTC)
        if payload.received_at > persisted or payload.event_time > payload.received_at:
            raise HTTPException(422, "Future event or receipt timestamp")
        with store.transaction() as db:
            current = store.selection(db)
            entity = store.entity(db, payload.entity_id)
            if (
                payload.selection_revision != current.revision
                or payload.entity_id not in current.entity_ids
                or entity is None
                or not entity.supported
            ):
                raise conflict("selection_changed", "Selection no longer permits this event")
            previous = db.execute(
                "SELECT data FROM events WHERE source_id=? AND id=?", (source_id, payload.id)
            ).fetchone()
            data = payload.model_dump(mode="json")
            for key in ("old", "new"):
                state = getattr(payload, key)
                data[key] = state.model_dump(mode="json", exclude_none=True) if state else None
            if previous:
                stored = EventRecord.model_validate_json(previous[0]).model_dump(
                    mode="json", exclude_unset=True
                )
                # A stable source ID cannot silently refer to different evidence.
                # The revision is admission context, not evidence: event IDs exclude
                # it, and the record keeps the revision it was first stored under.
                comparable = {
                    key: value
                    for key, value in stored.items()
                    if key not in ("persisted_at", "received_at", "selection_revision")
                }
                if comparable != {
                    key: value
                    for key, value in data.items()
                    if key
                    not in (
                        "received_at",
                        "selection_revision",
                        "kind",
                        "coverage_gap_ids",
                        "transition_time",
                        "collection_started_at",
                    )
                }:
                    raise conflict("event_identity_conflict", "Event identity conflict")
                return {"accepted": True, "recorded": False, "duplicate": True}
            old = payload.old.meaningful() if payload.old else None
            new = payload.new.meaningful() if payload.new else None
            if old == new:
                return {"accepted": True, "recorded": False, "duplicate": False}
            data["persisted_at"] = persisted.isoformat()
            db.execute(
                "INSERT INTO events VALUES(?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    payload.id,
                    payload.entity_id,
                    payload.event_time.isoformat(),
                    persisted.isoformat(),
                    json.dumps(data),
                ),
            )
            current_value = payload.new or LightState(state="unknown")
            if current_value:
                value = EventReading.model_validate(
                    current_value.model_dump(mode="json", exclude_none=True)
                    | {
                        "id": payload.id,
                        "entity_id": payload.entity_id,
                        "source_entity_id": payload.source_entity_id,
                        "source_id": source_id,
                        "kind": "event",
                        "event_time": payload.event_time.isoformat(),
                        "received_at": payload.received_at.isoformat(),
                        "persisted_at": persisted.isoformat(),
                    }
                )
                store.update_current(db, payload.entity_id, payload.event_time.isoformat(), value)
            # Older deliveries may add evidence but cannot refresh a newer confirmation backwards.
            confirmed = db.execute(
                "SELECT received_at FROM confirmed WHERE entity_id=?", (payload.entity_id,)
            ).fetchone()
            if confirmed is None or payload.received_at.isoformat() >= confirmed[0]:
                db.execute(
                    "INSERT OR REPLACE INTO confirmed VALUES(?, ?, ?)",
                    (payload.entity_id, payload.received_at.isoformat(), persisted.isoformat()),
                )
            return {"accepted": True, "recorded": True, "duplicate": False}

    @app.get("/api/events", response_model=EventsPage, response_model_exclude_unset=True)
    def events(
        *,
        source_id: Literal["demo", "home_assistant"] | None = None,
        entity_id: Identity | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=200)] = None,
    ):
        try:
            start = Observation.utc_timestamp(start)
            end = Observation.utc_timestamp(end)
            if start and end and start > end:
                raise ValueError("Reversed range")
            position = None
            if cursor is not None:
                timestamp, identity = cursor.split("|", 1)
                position = (to_utc(datetime.fromisoformat(timestamp)).isoformat(), identity)
                # Reuse the boundary type rather than accepting arbitrary cursor identifiers.
                position = (position[0], TypeAdapter(Identity).validate_python(identity))
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid event scope") from None
        clauses, parameters = ["persisted_at>=?"], [retention_cutoff()]
        for column, value in (("source_id", source_id), ("entity_id", entity_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        for operator, value in ((">=", start), ("<=", end)):
            if value is not None:
                clauses.append(f"event_time {operator} ?")
                parameters.append(value.isoformat())
        if position:
            clauses.append("(event_time < ? OR (event_time=? AND id < ?))")
            parameters.extend((position[0], position[0], position[1]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with store.transaction() as db:
            rows = db.execute(
                # Only fixed SQL fragments are concatenated; values are bound parameters.
                "SELECT data FROM events" + where + " ORDER BY event_time DESC, id DESC LIMIT ?",  # noqa: S608
                (*parameters, limit + 1),
            ).fetchall()
        values = [EventRecord.model_validate_json(row[0]) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = f"{values[-1].event_time}|{values[-1].id}"
        return EventsPage(
            events=values,
            next_cursor=next_cursor,
            retained_since=(now().astimezone(UTC) - timedelta(days=retention_days)).isoformat(),
            coverage="unknown",
        )

    @app.post("/internal/health", dependencies=[Depends(internal_auth)], include_in_schema=False)
    def ingest_health(payload: Health):
        if payload.source_id != source_id:
            raise conflict("source_mismatch", "Collection source mismatch")
        persisted = now().astimezone(UTC)
        if payload.received_at > persisted:
            raise HTTPException(422, "Future receipt timestamp")
        with store.transaction() as db:
            previous = store.health(db)
            # A delayed heartbeat cannot erase a newer failure. When the API's
            # clock rolled back, allow the new clock's heartbeat to reset health.
            if (
                previous
                and persisted >= datetime.fromisoformat(previous.persisted_at)
                and payload.received_at < datetime.fromisoformat(previous.received_at)
            ):
                raise conflict("heartbeat_older", "Heartbeat is older than latest health")
            if payload.status == "failed":
                reason = (
                    "invalid_data"
                    if payload.error == "discovery"
                    else payload.error or "connection"
                )
                store.interrupt(db, source_id, persisted.isoformat(), reason)
            if payload.status == "healthy":
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES('last_healthy_at', ?)",
                    (persisted.isoformat(),),
                )
            data = payload.model_dump(mode="json") | {"persisted_at": persisted.isoformat()}
            db.execute("INSERT OR REPLACE INTO metadata VALUES('health', ?)", (json.dumps(data),))
        return {"accepted": True}

    def get_health(db: sqlite3.Connection) -> HealthPending | HealthReport:
        result = store.health(db)
        if result is None:
            return HealthPending(status="pending", source_id=source_id)
        age = (now() - datetime.fromisoformat(result.received_at)).total_seconds()
        if age < 0 or age > 2 * reconciliation_seconds:
            result.status = "stale"
            store.interrupt(db, source_id, now().astimezone(UTC).isoformat(), "heartbeat")
        return result

    @app.post("/internal/coverage", dependencies=[Depends(internal_auth)], include_in_schema=False)
    def ingest_coverage(payload: Coverage):
        if payload.source_id != source_id:
            raise conflict("source_mismatch", "Collection source mismatch")
        persisted = now().astimezone(UTC)
        if payload.received_at > persisted:
            raise HTTPException(422, "Future receipt timestamp")
        with store.transaction() as db:
            current = store.selection(db)
            if (
                payload.entity_id is not None
                and payload.entity_id not in store.selection(db).entity_ids
            ):
                raise conflict(
                    "selection_changed", "Selection no longer permits this coverage scope"
                )
            if payload.status == "interrupted":
                gap = store.interrupt(
                    db, source_id, persisted.isoformat(), payload.reason, payload.entity_id
                )
                return {"accepted": True, "gap": gap}
            if payload.selection_revision != store.selection(db).revision:
                raise conflict("selection_changed", "Selection changed before recovery completed")
            recovered_entities = (
                payload.entity_ids if payload.entity_ids is not None else current.entity_ids
            )
            if not set(recovered_entities).issubset(current.entity_ids):
                raise conflict(
                    "selection_changed", "Selection no longer permits this recovery scope"
                )
            # Recovery is a collector assertion that subscription and snapshots completed.
            rows = db.execute(
                "SELECT id, scope, data FROM coverage WHERE recovered_at IS NULL"
            ).fetchall()
            relevant = [
                row
                for row in rows
                if (
                    row["scope"] == payload.entity_id
                    if payload.entity_id is not None
                    else row["scope"] == "*" or row["scope"] in recovered_entities
                )
            ]
            gaps = [CoverageGap.model_validate_json(row["data"]) for row in relevant]
            if any(payload.received_at < datetime.fromisoformat(gap.detected_at) for gap in gaps):
                raise conflict("recovery_older", "Recovery is older than interruption")
            for gap in gaps:
                gap.recovered_at = persisted.isoformat()
                db.execute(
                    "UPDATE coverage SET recovered_at=?, data=? WHERE id=?",
                    (persisted.isoformat(), gap.model_dump_json(), gap.id),
                )
            return {"accepted": True}

    @app.get("/api/coverage", response_model=CoverageReport)
    def coverage(limit: Annotated[int, Query(ge=1, le=100)] = 100):
        with store.transaction() as db:
            health = get_health(db)
            gaps = [
                CoverageGap.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT data FROM coverage WHERE recovered_at IS NULL OR recovered_at>=? "
                    "ORDER BY detected_at DESC, id DESC LIMIT ?",
                    (retention_cutoff(), limit),
                )
            ]
            return CoverageReport(
                gaps=gaps,
                state="interrupted"
                if any(
                    not gap.entity_ids or set(gap.entity_ids) & set(store.selection(db).entity_ids)
                    for gap in store.open_gaps(db)
                )
                else "collecting"
                if health.status == "healthy"
                else "unknown",
                last_confirmed_at=store.last_confirmed(db),
                retained_since=(now().astimezone(UTC) - timedelta(days=retention_days)).isoformat(),
                continuous_coverage=False,
                outside_recorded_windows="unknown",
            )

    @app.get("/api/usage", response_model=UsageReport)
    def usage(
        *,
        start: datetime,
        end: datetime,
        entity_id: Annotated[list[Identity] | None, Query(max_length=500)] = None,
    ):
        # A far-past offset can overflow the conversion to UTC.
        try:
            start = to_utc(start)
            end = to_utc(end)
        except (ValueError, OverflowError):
            raise HTTPException(422, "Invalid usage range") from None
        # Splitting by local day looks one day past `end`, which must stay a valid date.
        if start > end or end - start > timedelta(days=366) or end.year >= MAXYEAR:
            raise HTTPException(422, "Invalid usage range")
        cutoff = retention_cutoff()
        with store.transaction() as db:
            # An expired heartbeat becomes a recorded gap before coverage is read.
            get_health(db)
        # A deferred read sees one consistent state. It only copies raw rows; parsing and
        # computing happen after it closes. The store runs in WAL mode, so writers continue.
        with store.transaction("DEFERRED") as db:
            selected = store.selection(db).entity_ids
            # Unselected IDs are left out rather than reported as missing.
            lights = sorted(set(entity_id or selected) & set(selected))
            scope = ",".join("?" * len(lights))
            # Evidence inside the window, plus each light's latest evidence before it.
            snapshot_rows: list[sqlite3.Row] = db.execute(
                # Only placeholders are interpolated; values are bound parameters.
                "SELECT entity_id, data FROM baselines WHERE persisted_at>=? "  # noqa: S608 -- placeholders only
                "AND julianday(json_extract(data, '$.received_at')) <= julianday(?) "
                f"AND entity_id IN ({scope})",
                (max(cutoff, start.isoformat()), end.isoformat(), *lights),
            ).fetchall()
            event_rows: list[sqlite3.Row] = db.execute(
                "SELECT entity_id, data FROM events WHERE persisted_at>=? AND event_time>=? "  # noqa: S608 -- placeholders only
                f"AND event_time<=? AND entity_id IN ({scope})",
                (cutoff, start.isoformat(), end.isoformat(), *lights),
            ).fetchall()
            current_rows: list[sqlite3.Row] = []
            for light in lights:
                # Snapshots are stored in receipt order, so the newest persisted is the latest read.
                snapshot_rows += db.execute(
                    "SELECT entity_id, data FROM baselines WHERE entity_id=? AND persisted_at>=? "
                    "AND persisted_at<? ORDER BY persisted_at DESC, rowid DESC LIMIT 1",
                    (light, cutoff, start.isoformat()),
                ).fetchall()
                event_rows += db.execute(
                    "SELECT entity_id, data FROM events WHERE entity_id=? AND persisted_at>=? "
                    "AND event_time<? ORDER BY event_time DESC, id DESC LIMIT 1",
                    (light, cutoff, start.isoformat()),
                ).fetchall()
                # The cache keeps a steady light's state after its only snapshot expires.
                current_rows += db.execute(
                    "SELECT entity_id, data FROM current_state WHERE entity_id=?", (light,)
                ).fetchall()
            # Every retained gap: one that recovered before the window still leaves the light
            # unknown until new evidence arrives. Gaps are few, bounded by retention.
            # Also each scope's latest gap even when it has expired: cached state from before it
            # must not come back as known once the gap leaves retention.
            gap_rows: list[sqlite3.Row] = db.execute(
                "SELECT data FROM coverage WHERE recovered_at IS NULL OR recovered_at>=? "
                "OR recovered_at=(SELECT MAX(recovered_at) FROM coverage AS latest "
                "WHERE latest.scope=coverage.scope)",
                (cutoff,),
            ).fetchall()
        readings: dict[str, list[BaselineRecord | EventReading]] = {light: [] for light in lights}
        for row in snapshot_rows:
            readings[row[0]].append(BaselineRecord.model_validate_json(row[1]))
        for row in current_rows:
            readings[row[0]].append(current_reading.validate_json(row[1]))
        events: dict[str, list[EventRecord]] = {light: [] for light in lights}
        for row in event_rows:
            events[row[0]].append(EventRecord.model_validate_json(row[1]))
        gaps = [CoverageGap.model_validate_json(row[0]) for row in gap_rows]
        retained_since = datetime.fromisoformat(cutoff)
        return UsageReport(
            start=start.isoformat(),
            end=end.isoformat(),
            timezone=timezone.key,
            retained_since=cutoff,
            lights=[
                light_usage(
                    light,
                    start=start,
                    end=end,
                    now=now().astimezone(UTC),
                    timezone=timezone,
                    retained_since=retained_since,
                    readings=readings[light],
                    events=events[light],
                    gaps=gaps,
                )
                for light in lights
            ],
        )

    @app.get("/api/health", response_model=HealthStatus)
    def health():
        with store.transaction() as db:
            return get_health(db)

    @app.get("/api/monitored", response_model=MonitoredEntities, response_model_exclude_unset=True)
    def monitored():
        with store.transaction() as db:
            results: list[MonitoredEntity] = []
            health = get_health(db)
            for entity_id in store.selection(db).entity_ids:
                entity = store.entity(db, entity_id)
                missing = entity is None
                if entity is None:
                    entity = Entity(
                        id=entity_id,
                        name=entity_id,
                        device_id=None,
                        device_name=None,
                        supported=False,
                        reason="Entity missing from latest discovery",
                    )
                confirmed = db.execute(
                    "SELECT * FROM confirmed WHERE entity_id=?", (entity_id,)
                ).fetchone()
                freshness: Freshness = "pending"
                if confirmed:
                    receipt = datetime.fromisoformat(confirmed["received_at"])
                    persisted = datetime.fromisoformat(confirmed["persisted_at"])
                    fresh = (
                        0 <= (now() - receipt).total_seconds() <= 2 * reconciliation_seconds
                        and 0 <= (now() - persisted).total_seconds() <= 2 * reconciliation_seconds
                        and health.status not in ("failed", "stale")
                        and not store.open_gaps(db, entity_id)
                        and not missing
                        and entity.supported
                    )
                    freshness = "fresh" if fresh else "stale"
                results.append(
                    MonitoredEntity(
                        **entity.model_dump(),
                        baseline=store.current(db, entity_id) if confirmed else None,
                        freshness=freshness,
                        confirmed_at=confirmed["received_at"] if confirmed else None,
                    )
                )
            return MonitoredEntities(entities=results)

    web_path = Path.cwd() / "web"
    if web_path.exists():
        app.mount("/", StaticFiles(directory=web_path, html=True), name="dashboard")
    return app
