import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from home_observer.models import Catalog, Health, Observation, SelectionChange
from home_observer.store import Store


def create_app(
    db_path: Path,
    service_token: str,
    now=lambda: datetime.now(UTC),
    reconciliation_seconds: int = 60,
    allowed_hosts: tuple[str, ...] = ("testserver", "127.0.0.1:8000", "localhost:8000"),
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8000", "http://localhost:8000"),
):
    if reconciliation_seconds < 1 or not service_token:
        raise ValueError("Positive reconciliation interval and service credential required")
    store = Store(Path(db_path))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Host not allowed"}, status_code=400)
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD"):
            if (
                request.headers.get("x-home-observer") != "1"
                or request.headers.get("sec-fetch-site") == "cross-site"
                or (
                    request.headers.get("origin") is not None
                    and request.headers["origin"] not in allowed_origins
                )
            ):
                return JSONResponse({"detail": "Request origin denied"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def internal_auth(request: Request):
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode("utf-8"), f"Bearer {service_token}".encode()):
            raise HTTPException(401, "Service authentication required")

    # Validation details may contain arbitrary incoming text. Keep responses generic.

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.get("/api/catalog")
    def catalog():
        with store.transaction() as db:
            return {
                "entities": [
                    json.loads(row[0]) for row in db.execute("SELECT data FROM catalog ORDER BY id")
                ]
            }

    @app.get("/api/selection")
    @app.get("/internal/selection", dependencies=[Depends(internal_auth)])
    def selection():
        with store.transaction() as db:
            return store.selection(db)

    @app.put("/api/selection/{entity_id}")
    def select(entity_id: str, change: SelectionChange):
        with store.transaction() as db:
            entity = store.entity(db, entity_id)
            current = store.selection(db)
            if entity is None and (change.selected or entity_id not in current["entity_ids"]):
                raise HTTPException(404, "Unknown entity")
            if change.selected and not entity["supported"]:
                raise HTTPException(422, "Entity is unsupported")
            current = store.selection(db)
            if change.selected != (entity_id in current["entity_ids"]):
                if change.selected:
                    db.execute("INSERT INTO selection VALUES(?)", (entity_id,))
                    # A fresh selection awaits a new confirmation, even if history exists.
                    db.execute("DELETE FROM confirmed WHERE entity_id=?", (entity_id,))
                else:
                    db.execute("DELETE FROM selection WHERE id=?", (entity_id,))
                db.execute(
                    "UPDATE metadata SET value=? WHERE key='revision'",
                    (str(current["revision"] + 1),),
                )
            return store.selection(db)

    @app.post("/internal/catalog", dependencies=[Depends(internal_auth)])
    def ingest_catalog(payload: Catalog):
        with store.transaction() as db:
            db.execute("DELETE FROM catalog")
            db.executemany(
                "INSERT INTO catalog VALUES(?, ?)",
                [(e.id, e.model_dump_json()) for e in payload.entities],
            )
            # Discovery describes support; it never changes the user's approval.
        return {"accepted": True}

    @app.post("/internal/baselines", dependencies=[Depends(internal_auth)])
    def ingest_observation(payload: Observation):
        persisted = now().astimezone(UTC)
        if payload.received_at > persisted:
            raise HTTPException(422, "Future receipt timestamp")
        with store.transaction() as db:
            current = store.selection(db)
            entity = store.entity(db, payload.entity_id)
            if (
                payload.selection_revision != current["revision"]
                or payload.entity_id not in current["entity_ids"]
                or entity is None
                or not entity["supported"]
            ):
                raise HTTPException(409, "Selection no longer permits this observation")
            confirmed = db.execute(
                "SELECT received_at FROM confirmed WHERE entity_id=?", (payload.entity_id,)
            ).fetchone()
            if confirmed and payload.received_at < datetime.fromisoformat(confirmed[0]):
                raise HTTPException(409, "Observation is older than latest confirmation")
            data = payload.model_dump(mode="json", exclude={"selection_revision"})
            previous = db.execute(
                "SELECT data FROM baselines WHERE entity_id=? ORDER BY rowid DESC LIMIT 1",
                (payload.entity_id,),
            ).fetchone()
            baseline = json.loads(previous[0]) if previous else None
            comparable = {key: value for key, value in data.items() if key != "received_at"}
            old = {key: baseline.get(key) for key in comparable} if baseline else None
            if comparable != old:
                baseline = {**data, "id": str(uuid4()), "persisted_at": persisted.isoformat()}
                db.execute(
                    "INSERT INTO baselines VALUES(?, ?, ?)",
                    (baseline["id"], payload.entity_id, json.dumps(baseline)),
                )
            db.execute(
                "INSERT OR REPLACE INTO confirmed VALUES(?, ?, ?)",
                (payload.entity_id, payload.received_at.isoformat(), persisted.isoformat()),
            )
            return {"accepted": True, "baseline": baseline}

    @app.get("/api/baselines")
    def baselines(entity_id: str):
        with store.transaction() as db:
            return {
                "baselines": [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT data FROM baselines WHERE entity_id=? ORDER BY rowid", (entity_id,)
                    )
                ]
            }

    @app.post("/internal/health", dependencies=[Depends(internal_auth)])
    def ingest_health(payload: Health):
        persisted = now().astimezone(UTC)
        if payload.received_at > persisted:
            raise HTTPException(422, "Future receipt timestamp")
        with store.transaction() as db:
            previous = db.execute("SELECT value FROM metadata WHERE key='health'").fetchone()
            if previous:
                previous = json.loads(previous[0])
                # A delayed heartbeat cannot erase a newer failure. When the API's
                # clock rolled back, allow the new clock's heartbeat to reset health.
                if persisted >= datetime.fromisoformat(
                    previous["persisted_at"]
                ) and payload.received_at < datetime.fromisoformat(previous["received_at"]):
                    raise HTTPException(409, "Heartbeat is older than latest health")
            data = payload.model_dump(mode="json") | {"persisted_at": persisted.isoformat()}
            db.execute("INSERT OR REPLACE INTO metadata VALUES('health', ?)", (json.dumps(data),))
        return {"accepted": True}

    def get_health(db):
        row = db.execute("SELECT value FROM metadata WHERE key='health'").fetchone()
        if not row:
            return {"status": "pending", "source_id": "demo"}
        result = json.loads(row[0])
        age = (now() - datetime.fromisoformat(result["received_at"])).total_seconds()
        if age < 0 or age > 2 * reconciliation_seconds:
            result["status"] = "stale"
        return result

    @app.get("/api/health")
    def health():
        with store.transaction() as db:
            return get_health(db)

    @app.get("/api/monitored")
    def monitored():
        with store.transaction() as db:
            results = []
            health = get_health(db)
            for entity_id in store.selection(db)["entity_ids"]:
                entity = store.entity(db, entity_id)
                missing = entity is None
                if missing:
                    entity = {
                        "id": entity_id,
                        "name": entity_id,
                        "device_id": None,
                        "device_name": None,
                        "supported": False,
                        "reason": "Entity missing from latest discovery",
                    }
                row = db.execute(
                    "SELECT data FROM baselines WHERE entity_id=? ORDER BY rowid DESC LIMIT 1",
                    (entity_id,),
                ).fetchone()
                confirmed = db.execute(
                    "SELECT * FROM confirmed WHERE entity_id=?", (entity_id,)
                ).fetchone()
                freshness = "pending"
                if confirmed:
                    receipt = datetime.fromisoformat(confirmed["received_at"])
                    persisted = datetime.fromisoformat(confirmed["persisted_at"])
                    fresh = (
                        0 <= (now() - receipt).total_seconds() <= 2 * reconciliation_seconds
                        and 0 <= (now() - persisted).total_seconds() <= 2 * reconciliation_seconds
                        and health["status"] not in ("failed", "stale")
                        and not missing
                        and entity["supported"]
                    )
                    freshness = "fresh" if fresh else "stale"
                results.append(
                    entity
                    | {
                        "baseline": json.loads(row[0]) if row and confirmed else None,
                        "freshness": freshness,
                        "confirmed_at": confirmed["received_at"] if confirmed else None,
                    }
                )
            return {"entities": results}

    web_path = Path.cwd() / "web"
    if web_path.exists():
        app.mount("/", StaticFiles(directory=web_path, html=True), name="dashboard")
    return app
