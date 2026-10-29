"""Trusted, fixed-read Home Assistant adapter. Raw registry and state data never leave it."""

# SelectedSubscription is the connector's companion and reuses its private session state.
# pyright: reportPrivateUsage=false

import contextlib
import ipaddress
import itertools
import json
import logging
import re
import ssl
import time
from collections.abc import Generator, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter
from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from home_keeper.events import change_event, device_state, is_json_object
from home_keeper.models import (
    Catalog,
    ChangeEvent,
    DeviceObservation,
    Entity,
    LightState,
    SwitchState,
    device_kind_of,
)

MAX_PAYLOAD = 2 * 1024 * 1024
# Weeks of a busy light's history outgrow one registry list.
HISTORY_MAX_PAYLOAD = 16 * 1024 * 1024
MAX_REGISTRY_ROWS = 500
MAX_SOURCE_ENTITY_ID_LENGTH = 128
MAX_TOKEN_LENGTH = 4096
MAX_TIMEOUT_SECONDS = 30
MAX_RECEIVE_TIMEOUT_SECONDS = 5
# Visible ASCII only: no spaces, control characters or non-ASCII in origins and credentials.
VISIBLE_ASCII = range(33, 127)
_REGISTRY_ID = re.compile(r"^[a-zA-Z0-9_-]{1,120}$")
_DEVICE_ID = re.compile(r"^(light|switch)\.[a-z0-9_]{1,120}$")
# Integrations flag their group entities with attributes such as is_hue_group or is_deconz_group.
_GROUP_FLAG = re.compile(r"^is_[a-z0-9_]+_group$")
# The only WebSocket requests the adapter sends: reads, never service calls or writes.
_READ_COMMANDS = frozenset(
    {
        "config/entity_registry/list",
        "config/device_registry/list",
        "history/history_during_period",
    }
)
GROUP_REASON = "Groups and rooms are not recorded"
DISABLED_REASON = "Disabled in Home Assistant"
NOT_A_DEVICE_REASON = "Only lights, plugs and switches are supported"
SWITCH_CATEGORY_REASON = "Settings and diagnostic switches are not recorded"
SWITCH_DEVICE_REASON = "Switches without a device are not recorded"
_observation: TypeAdapter[DeviceObservation] = TypeAdapter(DeviceObservation)
_WS_LOGGER = logging.getLogger("home_keeper.transport.private")
_WS_LOGGER.addHandler(logging.NullHandler())
_WS_LOGGER.propagate = False
_WS_LOGGER.setLevel(logging.CRITICAL + 1)


# Approved (identity, revision, source entity) triples a subscription was opened for.
type SubscriptionKey = tuple[tuple[str, int, str], ...]


class WebSocket(Protocol):
    """The part of a websockets sync connection the adapter uses."""

    def send(self, message: str, /) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...


class ConnectorError(Exception):
    """Contains a fixed safe code, never upstream exception or response text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Home Assistant collection failed ({code})")


class Subscription(Protocol):
    key: SubscriptionKey

    def receive(self, timeout: float = 1) -> ChangeEvent | None: ...
    def close(self) -> None: ...


class Connector(Protocol):
    def discover(self) -> Catalog: ...
    def approve(self, selected: dict[str, int]) -> None: ...
    def read_selected(self, identity: str, revision: int) -> DeviceObservation: ...
    @property
    def subscription_key(self) -> SubscriptionKey: ...
    def subscribe_selected(self) -> Subscription | None: ...
    def close(self) -> None: ...


class HomeAssistantConnector:
    def __init__(
        self,
        origin: str,
        token_file: Path,
        timeout_seconds: float = 5,
        redaction_values: tuple[str, ...] = (),
    ):
        try:
            if any(ord(character) not in VISIBLE_ASCII for character in origin):
                raise ValueError
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or "\\" in parsed.netloc
                or parsed.netloc.endswith(":")
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
            ):
                raise ValueError
            _ = parsed.port  # Raises ValueError for an out-of-range or non-numeric port.
            if ":" in parsed.hostname:
                ipaddress.ip_address(parsed.hostname)
            elif not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname):
                raise ValueError
            token = token_file.read_text().strip()
            if (
                not token
                or len(token) > MAX_TOKEN_LENGTH
                or not token.isascii()
                or any(ord(character) not in VISIBLE_ASCII for character in token)
            ):
                raise ValueError
        except (ValueError, OSError):
            raise ConnectorError("configuration") from None
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self._websocket_url = (
            f"{'wss' if parsed.scheme == 'https' else 'ws'}://{parsed.netloc}/api/websocket"
        )
        self._token = token
        self._redactions = (token, *redaction_values)
        self._timeout = timeout_seconds
        self._http = httpx.Client(
            base_url=self.origin,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
            verify=True,
            trust_env=False,
            follow_redirects=False,
        )
        self._entities: dict[str, str] = {}
        self._supported: set[str] = set()
        self._approved: dict[str, int] = {}
        self._validated = False
        # Whether each light or switch candidate's state marks it as a group, by source entity
        # ID. Read once per candidate, so a steady home costs no state reads.
        self._groups: dict[str, bool] = {}

    def close(self) -> None:
        self._http.close()

    def discover(self) -> Catalog:
        # A failed refresh forbids reading even cached identities until revalidated.
        self._validated = False
        self._approved = {}
        with self._session() as session:
            entities = _rows(session.request("config/entity_registry/list", failure="discovery"))
            devices = _rows(session.request("config/device_registry/list", failure="discovery"))
            catalog, mapping = self._project(entities, devices)
        self._entities = mapping
        self._supported = {entity.id for entity in catalog.entities if entity.supported}
        self._validated = True
        return catalog

    def history(
        self, source_entity_ids: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, list[LightState | SwitchState]]:
        """Each light's or switch's recorded states from `start` to `end`, oldest first, in one
        request. The first is its state at `start`; an entity with no history has []."""
        if any(not _DEVICE_ID.fullmatch(entity_id) for entity_id in source_entity_ids):
            raise ConnectorError("configuration")
        if not source_entity_ids:
            return {}
        with self._session(HISTORY_MAX_PAYLOAD) as session:
            result = session.request(
                "history/history_during_period",
                # Home Assistant answers a malformed history request with an error result.
                failure="invalid_data",
                start_time=start.isoformat(),
                end_time=end.isoformat(),
                entity_ids=list(source_entity_ids),
                include_start_time_state=True,
                # Lights are not in Home Assistant's significant domains: without this, it
                # leaves out their brightness and color changes.
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=False,
            )
            if not is_json_object(result):
                raise ConnectorError("invalid_data")
            return {
                entity_id: [
                    history_state(row, entity_id) for row in _rows(result.get(entity_id, []))
                ]
                for entity_id in source_entity_ids
            }

    @contextlib.contextmanager
    def _session(self, max_size: int = MAX_PAYLOAD) -> Generator["Session"]:
        """An authenticated WebSocket for read requests. Every failure inside, including the
        caller's parsing, leaves as a safe ConnectorError code."""
        try:
            with connect(
                self._websocket_url,
                open_timeout=self._timeout,
                close_timeout=self._timeout,
                max_size=max_size,
                proxy=None,
                logger=_WS_LOGGER,
            ) as socket:
                if self._receive(socket, max_size).get("type") != "auth_required":
                    raise ConnectorError("invalid_data")
                socket.send(json.dumps({"type": "auth", "access_token": self._token}))
                authentication = self._receive(socket, max_size).get("type")
                if authentication == "auth_invalid":
                    raise ConnectorError("authentication")
                if authentication != "auth_ok":
                    raise ConnectorError("invalid_data")
                yield Session(self, socket, max_size)
        except Exception as error:
            _raise_safe(error)

    def _receive(self, socket: WebSocket, max_size: int = MAX_PAYLOAD) -> dict[str, Any]:
        raw = socket.recv(timeout=self._timeout)
        if len(raw) > max_size:
            raise ConnectorError("invalid_data")
        data = json.loads(raw)
        if not is_json_object(data):
            raise ConnectorError("invalid_data")
        return data

    def _name(self, value: object, fallback: str) -> str:
        name = value if isinstance(value, str) and value else fallback
        for secret in self._redactions:
            if secret:
                name = name.replace(secret, "[redacted]")
        return name[:256]

    def _project(self, entities: list[Any], devices: list[Any]) -> tuple[Catalog, dict[str, str]]:
        if len(entities) > MAX_REGISTRY_ROWS or len(devices) > MAX_REGISTRY_ROWS:
            raise ConnectorError("invalid_data")
        device_names: dict[str, str] = {}
        for device in devices:
            identity = device.get("id") if is_json_object(device) else None
            if not isinstance(identity, str) or not _REGISTRY_ID.fullmatch(identity):
                raise ConnectorError("invalid_data")
            device_names[identity] = self._name(
                device.get("name_by_user") or device.get("name"), "Unnamed device"
            )
        projected: list[Entity] = []
        mapping: dict[str, str] = {}
        for entity in entities:
            if not is_json_object(entity):
                raise ConnectorError("invalid_data")
            registry_id = entity.get("id")
            entity_id = entity.get("entity_id")
            if (
                not isinstance(registry_id, str)
                or not _REGISTRY_ID.fullmatch(registry_id)
                or not isinstance(entity_id, str)
                or len(entity_id) > MAX_SOURCE_ENTITY_ID_LENGTH
            ):
                raise ConnectorError("invalid_data")
            identity = f"ha:{registry_id}"
            device_id = entity.get("device_id")
            if device_id is not None and device_id not in device_names:
                # Dangling relationships retain the entity without inventing a device.
                device_id = None
            kind = device_kind_of(entity_id)
            if not _DEVICE_ID.fullmatch(entity_id):
                reason = NOT_A_DEVICE_REASON
            elif entity.get("platform") == "group":
                reason = GROUP_REASON
            elif entity.get("disabled_by") is not None:
                reason = DISABLED_REASON
            # A switch outside a device, or one in a settings category, is a helper or an
            # integration option, not a plug or switch someone uses.
            elif kind == "switch" and entity.get("entity_category") is not None:
                reason = SWITCH_CATEGORY_REASON
            elif kind == "switch" and not device_id:
                reason = SWITCH_DEVICE_REASON
            # Only a candidate that passes every other rule has its state read.
            elif self._is_group(entity_id):
                reason = GROUP_REASON
            else:
                reason = None
            supported = reason is None
            projected.append(
                Entity(
                    id=identity,
                    name=self._name(
                        entity.get("name") or entity.get("original_name"), "Unnamed entity"
                    ),
                    device_id=f"ha:{device_id}" if device_id else None,
                    device_name=device_names.get(device_id) if device_id else None,
                    device_kind=kind,
                    supported=supported,
                    reason=reason,
                )
            )
            mapping[identity] = entity_id
        sources = set(mapping.values())
        self._groups = {
            entity_id: group for entity_id, group in self._groups.items() if entity_id in sources
        }
        return Catalog(source_id="home_assistant", entities=projected), mapping

    def _is_group(self, entity_id: str) -> bool:
        """Whether a candidate's state marks it as a group. Keeps only that answer, and only
        when the read succeeds."""
        if entity_id in self._groups:
            return self._groups[entity_id]
        try:
            status, payload = self._read_state(entity_id)
            if status == HTTPStatus.NOT_FOUND:
                # No state yet, so no group markers.
                self._groups[entity_id] = False
                return False
            if status != HTTPStatus.OK:
                raise ConnectorError("discovery")
            data = json.loads(payload)
        except ConnectorError:
            raise
        except (httpx.HTTPError, TimeoutError):
            raise ConnectorError("discovery") from None
        except ValueError:
            raise ConnectorError("invalid_data") from None
        if not is_json_object(data) or data.get("entity_id") != entity_id:
            raise ConnectorError("invalid_data")
        attributes = data.get("attributes")
        if not is_json_object(attributes):
            raise ConnectorError("invalid_data")
        group = isinstance(attributes.get("entity_id"), list) or any(
            value is True and _GROUP_FLAG.fullmatch(key) for key, value in attributes.items()
        )
        self._groups[entity_id] = group
        return group

    def _read_state(self, entity_id: str) -> tuple[int, bytes]:
        """GET one entity's state: the status, and the body only for 200. Raises TimeoutError
        when the body outlasts the timeout and ConnectorError when it exceeds MAX_PAYLOAD."""
        deadline = time.monotonic() + self._timeout
        with self._http.stream("GET", f"/api/states/{entity_id}") as response:
            if response.status_code != HTTPStatus.OK:
                return response.status_code, b""
            payload = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    raise TimeoutError
                payload.extend(chunk)
                if len(payload) > MAX_PAYLOAD:
                    raise ConnectorError("invalid_data")
        return HTTPStatus.OK, bytes(payload)

    def approve(self, selected: dict[str, int]) -> None:
        if not self._validated or any(
            identity not in self._supported
            # Defense in depth: approvals must be integer revisions even from untyped callers.
            or not isinstance(revision, int)  # pyright: ignore[reportUnnecessaryIsInstance]
            or revision < 0
            for identity, revision in selected.items()
        ):
            raise ConnectorError("configuration")
        self._approved = dict(selected)

    @property
    def subscription_key(self) -> SubscriptionKey:
        if not self._validated:
            return ()
        return tuple(
            sorted(
                (identity, revision, self._entities[identity])
                for identity, revision in self._approved.items()
            )
        )

    def subscribe_selected(self) -> "SelectedSubscription | None":
        if not self.subscription_key:
            return None
        return SelectedSubscription(self)

    def read_selected(self, identity: str, revision: int) -> DeviceObservation:
        if (
            not self._validated
            or identity not in self._supported
            or self._approved.get(identity) != revision
        ):
            raise ConnectorError("configuration")
        entity_id = self._entities[identity]
        started_at = datetime.now(UTC)
        try:
            status, payload = self._read_state(entity_id)
            if status in {401, 403}:
                raise ConnectorError("authentication")
            if status == HTTPStatus.NOT_FOUND:
                raise ConnectorError("missing")
            if status != HTTPStatus.OK:
                raise ConnectorError("connection")
            data = json.loads(payload)
            value = device_state(data, entity_id)
            if value is None:
                raise ConnectorError("invalid_data")
            return _observation.validate_python(
                value.model_dump(exclude_none=True)
                | {
                    "entity_id": identity,
                    "source_entity_id": entity_id,
                    "source_id": "home_assistant",
                    "selection_revision": revision,
                    "received_at": datetime.now(UTC),
                    "collection_started_at": started_at,
                }
            )

        except ConnectorError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError) as error:
            raise _read_failure(error) from None

    @property
    def source_entity_ids(self) -> dict[str, str]:
        """Each identity's Home Assistant entity ID, from the latest successful discovery."""
        return dict(self._entities)

    def read_state(self, source_entity_id: str) -> LightState | SwitchState | None:
        """The current state of a light or switch; None when Home Assistant has no such
        entity. Unlike `read_selected`, it needs no approval: the reader serves the API,
        which checks the selection itself."""
        if not _DEVICE_ID.fullmatch(source_entity_id):
            raise ConnectorError("configuration")
        try:
            status, payload = self._read_state(source_entity_id)
            if status in {401, 403}:
                raise ConnectorError("authentication")
            if status == HTTPStatus.NOT_FOUND:
                return None
            if status != HTTPStatus.OK:
                raise ConnectorError("connection")
            value = device_state(json.loads(payload), source_entity_id)
        except ConnectorError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError) as error:
            raise _read_failure(error) from None
        if value is None:
            raise ConnectorError("invalid_data")
        return value

    def check(self) -> None:
        """Return when Home Assistant's API answers and accepts the token."""
        try:
            response = self._http.get("/api/")
        except (httpx.HTTPError, TimeoutError) as error:
            raise _read_failure(error) from None
        if response.status_code in {401, 403}:
            raise ConnectorError("authentication")
        if response.status_code != HTTPStatus.OK:
            raise ConnectorError("connection")


def _read_failure(error: Exception) -> ConnectorError:
    """The safe code for a failed HTTP read: a type check, never the error's text."""
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return ConnectorError("timeout")
    if isinstance(error, httpx.HTTPError):
        # httpx wraps TLS errors; inspect types, never stringify secrets-bearing causes.
        cause: BaseException | None = error
        while cause is not None:
            if isinstance(cause, ssl.SSLError):
                return ConnectorError("tls")
            cause = cause.__cause__
        return ConnectorError("connection")
    # ValueError covers pydantic's ValidationError and JSON decoding.
    return ConnectorError("invalid_data")


def _raise_safe(error: Exception) -> NoReturn:
    """The safe code for a failed WebSocket exchange: a type check, never the error's text."""
    if isinstance(error, ConnectorError):
        raise error
    if isinstance(error, ssl.SSLError):
        raise ConnectorError("tls") from None
    if isinstance(error, TimeoutError):
        raise ConnectorError("timeout") from None
    if isinstance(error, (OSError, WebSocketException)):
        raise ConnectorError("connection") from None
    raise ConnectorError("invalid_data") from None


class Session:
    """Read requests on one authenticated WebSocket. Home Assistant needs each request's ID to
    be higher than the last, so the session numbers them from 1."""

    def __init__(self, connector: HomeAssistantConnector, socket: WebSocket, max_size: int):
        self._connector = connector
        self._socket = socket
        self._max_size = max_size
        self._ids = itertools.count(1)

    def request(self, command: str, *, failure: str, **fields: object) -> object:
        """Send one allowlisted read and return its result. A refusal raises `failure`."""
        # Guarded here too: no caller can add a write command.
        if command not in _READ_COMMANDS:
            raise ConnectorError("configuration")
        request_id = next(self._ids)
        self._socket.send(json.dumps({**fields, "id": request_id, "type": command}))
        response = self._connector._receive(self._socket, self._max_size)
        if response.get("type") != "result" or response.get("id") != request_id:
            raise ConnectorError("invalid_data")
        if response.get("success") is not True:
            raise ConnectorError(failure)
        return response.get("result")


def _rows(value: object) -> list[object]:
    """A raw list from Home Assistant; its callers validate each row."""
    if not isinstance(value, list):
        raise ConnectorError("invalid_data")
    return cast("list[object]", value)


def _timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("Invalid history timestamp")
    return datetime.fromtimestamp(value, UTC)


def history_state(row: object, source_entity_id: str) -> LightState | SwitchState:
    """One compressed history row, `{s, a, lc, lu}`, as a light's or switch's state. Times are
    Unix seconds; Home Assistant leaves out `lu` when it equals `lc`. Only the approved light
    attributes are kept; a switch keeps its state only."""
    if not is_json_object(row):
        raise ValueError("Invalid history row")
    changed = _timestamp(row.get("lc"))
    state = device_state(
        {
            "entity_id": source_entity_id,
            "state": row.get("s"),
            "attributes": row.get("a", {}),
            "last_changed": changed,
            "last_updated": _timestamp(row["lu"]) if "lu" in row else changed,
        },
        source_entity_id,
    )
    if state is None:
        raise ValueError("Invalid history row")
    return state


class SelectedSubscription:
    """Only fixed state_changed subscriptions, with filtering before projection."""

    def __init__(self, connector: HomeAssistantConnector):
        self._connector = connector
        self.key = connector.subscription_key
        if not self.key:
            raise ConnectorError("configuration")
        self._mapping = {source: (identity, revision) for identity, revision, source in self.key}
        self._socket: ClientConnection | None = None
        try:
            self._socket = connect(
                connector._websocket_url,
                open_timeout=connector._timeout,
                close_timeout=connector._timeout,
                max_size=MAX_PAYLOAD,
                max_queue=16,
                proxy=None,
                logger=_WS_LOGGER,
            )
            if connector._receive(self._socket).get("type") != "auth_required":
                raise ConnectorError("invalid_data")
            self._socket.send(json.dumps({"type": "auth", "access_token": connector._token}))
            auth = connector._receive(self._socket).get("type")
            if auth == "auth_invalid":
                raise ConnectorError("authentication")
            if auth != "auth_ok":
                raise ConnectorError("invalid_data")
            self._socket.send(
                json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"})
            )
            result = connector._receive(self._socket)
            if result.get("type") != "result" or result.get("id") != 1:
                raise ConnectorError("invalid_data")
            if result.get("success") is not True:
                raise ConnectorError("discovery")
        except Exception as error:
            self.close()
            _raise_safe(error)

    def receive(self, timeout: float = 1) -> ChangeEvent | None:
        if not 0 < timeout <= MAX_RECEIVE_TIMEOUT_SECONDS:
            raise ConnectorError("configuration")
        # Approvals are refreshed by the collector. Do not drain old permissions.
        if self._connector._validated and self.key != self._connector.subscription_key:
            return None
        try:
            if self._socket is None:
                raise ConnectorError("invalid_data")
            try:
                raw = self._socket.recv(timeout=timeout)
            except TimeoutError:
                return None
            if len(raw) > MAX_PAYLOAD:
                raise ConnectorError("invalid_data")
            message = json.loads(raw)
            if (
                not is_json_object(message)
                or message.get("type") != "event"
                or message.get("id") != 1
            ):
                raise ConnectorError("invalid_data")
            envelope = message.get("event")
            if not is_json_object(envelope) or envelope.get("event_type") != "state_changed":
                raise ConnectorError("invalid_data")
            data = envelope.get("data")
            if not is_json_object(data):
                raise ConnectorError("invalid_data")
            source_entity_id = data.get("entity_id")
            if not isinstance(source_entity_id, str) or source_entity_id not in self._mapping:
                return None
            identity, revision = self._mapping[source_entity_id]
            old = device_state(data.get("old_state"), source_entity_id)
            new = device_state(data.get("new_state"), source_entity_id)
            fired = datetime.fromisoformat(envelope["time_fired"])
            if fired.tzinfo is None or fired.utcoffset() is None:
                raise ConnectorError("invalid_data")
            return change_event(
                source_id="home_assistant",
                entity_id=identity,
                source_entity_id=source_entity_id,
                revision=revision,
                event_time=fired.astimezone(UTC),
                old=old,
                new=new,
            )
        except Exception as error:
            _raise_safe(error)

    def close(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(OSError, WebSocketException):
                self._socket.close()
            self._socket = None
