"""Trusted, fixed-read Home Assistant adapter. Raw registry and state data never leave it."""

import contextlib
import ipaddress
import itertools
import json
import logging
import re
import ssl
import time
from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, NoReturn, Protocol, cast
from urllib.parse import urlsplit

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from home_keeper.events import device_state, is_json_object
from home_keeper.models import (
    MAX_CATALOG_ENTITIES,
    PRESENCE_DOMAINS,
    Catalog,
    DeviceState,
    Entity,
    device_kind_of,
)

# One WebSocket message: a whole registry list in one answer. Real registry rows are about
# 400 to 600 bytes, so MAX_REGISTRY_ROWS of them fit with room for rows that run larger.
MAX_PAYLOAD = 8 * 1024 * 1024
# Weeks of a busy light's history outgrow one registry list.
HISTORY_MAX_PAYLOAD = 16 * 1024 * 1024
# Every entity's state in one answer, sensors included.
STATES_MAX_PAYLOAD = 16 * 1024 * 1024
MAX_REGISTRY_ROWS = MAX_CATALOG_ENTITIES
MAX_SOURCE_ENTITY_ID_LENGTH = 128
# Home Assistant's own cap on a state; a translated word is held to it too.
MAX_STATE_LENGTH = 255
MAX_TOKEN_LENGTH = 4096
MAX_TIMEOUT_SECONDS = 30
# Visible ASCII only: no spaces, control characters or non-ASCII in origins and credentials.
VISIBLE_ASCII = range(33, 127)
_REGISTRY_ID = re.compile(r"^[a-zA-Z0-9_-]{1,120}$")
# Any `<domain>.<object_id>`, at most MAX_SOURCE_ENTITY_ID_LENGTH long.
_DEVICE_ID = re.compile(r"^(?=.{1,128}$)[a-z0-9_]+\.[a-z0-9_]{1,120}$")
# Home Assistant's language code, such as `en` or `pt-BR`.
_LANGUAGE = re.compile(r"^[A-Za-z0-9_-]{1,35}$")
# A translated state word: `component.<domain>.entity_component.<device class or _>.state.<state>`.
_STATE_WORD = re.compile(r"^component\.([a-z0-9_]+)\.entity_component\.([a-z0-9_]+)\.state\.(.+)$")
# The longest device class kept for a translation lookup.
MAX_DEVICE_CLASS_LENGTH = 64
# Integrations flag their group entities with attributes such as is_hue_group or is_deconz_group.
_GROUP_FLAG = re.compile(r"^is_[a-z0-9_]+_group$")
# The only WebSocket requests the adapter sends: reads, never service calls or writes.
_READ_COMMANDS = frozenset(
    {
        "config/entity_registry/list",
        "config/device_registry/list",
        "history/history_during_period",
        "get_config",
        "frontend/get_translations",
    }
)
GROUP_REASON = "Groups and rooms are not recorded"
DISABLED_REASON = "Disabled in Home Assistant"
PRESENCE_REASON = "Presence is not recorded"
CATEGORY_REASON = "Settings and diagnostic entities are not recorded"
HIDDEN_REASON = "Hidden in Home Assistant"
NO_DEVICE_REASON = "Only entities of a device are recorded"
# Home Assistant's own entity IDs always match _DEVICE_ID; this is for one that does not.
ENTITY_ID_REASON = "Entity ID is not supported"
SWITCH_CATEGORY_REASON = "Settings and diagnostic switches are not recorded"
SWITCH_DEVICE_REASON = "Switches without a device are not recorded"
_WS_LOGGER = logging.getLogger("home_keeper.transport.private")
_WS_LOGGER.addHandler(logging.NullHandler())
_WS_LOGGER.propagate = False
_WS_LOGGER.setLevel(logging.CRITICAL + 1)


class WebSocket(Protocol):
    """The part of a websockets sync connection the adapter uses."""

    def send(self, message: str, /) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...


@dataclass(frozen=True)
class Labels:
    """Home Assistant's translated state words, in its language, from discovery."""

    # Each `(domain, device class or "_", state)` with its word.
    words: Mapping[tuple[str, str, str], str]
    # Each device's device class, by Home Assistant entity ID, where it has one.
    device_classes: Mapping[str, str]

    def of(self, source_entity_id: str, state: str) -> str:
        """The word for a state: by the device's class first, then the domain's own; else the
        state itself."""
        domain = source_entity_id.partition(".")[0]
        device_class = self.device_classes.get(source_entity_id)
        for key in (device_class, "_"):
            if key is not None and (word := self.words.get((domain, key, state))):
                return word
        return state


class ConnectorError(Exception):
    """Contains a fixed safe code, never upstream exception or response text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Home Assistant collection failed ({code})")


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
        self._labels = Labels({}, {})
        # Device classes from the latest state read of a discovery.
        self._device_classes: dict[str, str] = {}

    def close(self) -> None:
        self._http.close()

    def discover(self) -> Catalog:
        self._device_classes = {}
        with self._session() as session:
            entities = _rows(session.request("config/entity_registry/list", failure="discovery"))
            devices = _rows(session.request("config/device_registry/list", failure="discovery"))
            catalog, mapping = self._project(entities, devices)
            words = _state_words(session)
        self._entities = mapping
        self._labels = Labels(words, self._device_classes)
        return catalog

    def history(
        self, source_entity_ids: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, list[DeviceState]]:
        """Each device's recorded states from `start` to `end`, oldest first. The first is its
        state at `start`; an entity with no history has []."""
        if any(not _DEVICE_ID.fullmatch(entity_id) for entity_id in source_entity_ids):
            raise ConnectorError("configuration")
        if not source_entity_ids:
            return {}
        # Home Assistant takes one `no_attributes` per request: lights need their attributes,
        # and every other device's are never kept, so those are read without them.
        attributed = ("light", "switch")
        with_attributes = [i for i in source_entity_ids if device_kind_of(i) in attributed]
        without = [i for i in source_entity_ids if device_kind_of(i) not in attributed]
        history: dict[str, list[DeviceState]] = {}
        with self._session(HISTORY_MAX_PAYLOAD) as session:
            for entity_ids, no_attributes in ((with_attributes, False), (without, True)):
                if not entity_ids:
                    continue
                result = session.request(
                    "history/history_during_period",
                    # Home Assistant answers a malformed history request with an error result.
                    failure="invalid_data",
                    start_time=start.isoformat(),
                    end_time=end.isoformat(),
                    entity_ids=entity_ids,
                    include_start_time_state=True,
                    # Lights are not in Home Assistant's significant domains: without this, it
                    # leaves out their brightness and color changes.
                    significant_changes_only=False,
                    minimal_response=False,
                    no_attributes=no_attributes,
                )
                if not is_json_object(result):
                    raise ConnectorError("invalid_data")
                history |= {
                    entity_id: [
                        self._labelled(history_state(row, entity_id), entity_id)
                        for row in _rows(result.get(entity_id, []))
                    ]
                    for entity_id in entity_ids
                }
        return {entity_id: history[entity_id] for entity_id in source_entity_ids}

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
        # Read on the first candidate, so a home without one costs no state read.
        groups: frozenset[str] | None = None
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
                kind = None
                reason = ENTITY_ID_REASON
            elif kind in ("light", "switch"):
                reason = _light_or_switch_reason(entity, kind, device_id)
            else:
                reason = _other_reason(entity, entity_id, device_id)
            # Only a candidate that passes every other rule needs its state.
            if reason is None:
                if groups is None:
                    groups = self._read_groups()
                reason = GROUP_REASON if entity_id in groups else None
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
        return Catalog(source_id="home_assistant", entities=projected), mapping

    def _read_groups(self) -> frozenset[str]:
        """The devices whose state marks them as a group, from one read of every state. An
        entity with no state has no group markers. Keeps each device's device class for
        translating its state."""
        try:
            status, payload = self._get("/api/states", STATES_MAX_PAYLOAD)
            if status != HTTPStatus.OK:
                raise ConnectorError("discovery")
            rows = json.loads(payload)
        except ConnectorError:
            raise
        except (httpx.HTTPError, TimeoutError):
            raise ConnectorError("discovery") from None
        except ValueError:
            raise ConnectorError("invalid_data") from None
        groups: set[str] = set()
        for row in _rows(rows):
            if not is_json_object(row):
                raise ConnectorError("invalid_data")
            entity_id = row.get("entity_id")
            # Only devices can be candidates; presence and malformed IDs are skipped.
            if (
                not isinstance(entity_id, str)
                or not _DEVICE_ID.fullmatch(entity_id)
                or device_kind_of(entity_id) is None
            ):
                continue
            attributes = row.get("attributes")
            if not is_json_object(attributes):
                raise ConnectorError("invalid_data")
            device_class = attributes.get("device_class")
            if (
                isinstance(device_class, str)
                and 0 < len(device_class) <= MAX_DEVICE_CLASS_LENGTH
                and device_class.isascii()
            ):
                self._device_classes[entity_id] = device_class
            if isinstance(attributes.get("entity_id"), list) or any(
                value is True and _GROUP_FLAG.fullmatch(key) for key, value in attributes.items()
            ):
                groups.add(entity_id)
        return frozenset(groups)

    def _get(self, path: str, max_size: int) -> tuple[int, bytes]:
        """The status, and the body only for 200. Raises TimeoutError when the body outlasts
        the timeout and ConnectorError when it exceeds `max_size`."""
        deadline = time.monotonic() + self._timeout
        with self._http.stream("GET", path) as response:
            if response.status_code != HTTPStatus.OK:
                return response.status_code, b""
            payload = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    raise TimeoutError
                payload.extend(chunk)
                if len(payload) > max_size:
                    raise ConnectorError("invalid_data")
        return HTTPStatus.OK, bytes(payload)

    @property
    def source_entity_ids(self) -> dict[str, str]:
        """Each identity's Home Assistant entity ID, from the latest successful discovery."""
        return dict(self._entities)

    def _labelled[State: DeviceState](self, state: State, source_entity_id: str) -> State:
        """The state with its word, from the translations the latest discovery read."""
        return state.model_copy(update={"label": self._labels.of(source_entity_id, state.state)})

    def read_states(self, source_entity_ids: Sequence[str]) -> dict[str, DeviceState | None]:
        """The current state of each device, from one read of every state; None when Home
        Assistant has no such entity. The API checks the selection itself."""
        wanted = set(source_entity_ids)
        if not all(_DEVICE_ID.fullmatch(entity_id) for entity_id in wanted):
            raise ConnectorError("configuration")
        try:
            status, payload = self._get("/api/states", STATES_MAX_PAYLOAD)
            if status in {401, 403}:
                raise ConnectorError("authentication")
            if status != HTTPStatus.OK:
                raise ConnectorError("connection")
            rows = json.loads(payload)
            if not isinstance(rows, list):
                raise TypeError("Not a list of states")
            found: dict[str, DeviceState | None] = {}
            for row in cast("list[object]", rows):
                if not is_json_object(row):
                    raise TypeError("Not a state")
                entity_id = row.get("entity_id")
                if isinstance(entity_id, str) and entity_id in wanted:
                    state = device_state(row, entity_id)
                    found[entity_id] = state and self._labelled(state, entity_id)
        except ConnectorError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError) as error:
            raise _read_failure(error) from None
        return {entity_id: found.get(entity_id) for entity_id in source_entity_ids}

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
        # The session is the connector's companion and shares its size-checked receive.
        response = self._connector._receive(self._socket, self._max_size)  # pyright: ignore[reportPrivateUsage]
        if response.get("type") != "result" or response.get("id") != request_id:
            raise ConnectorError("invalid_data")
        if response.get("success") is not True:
            raise ConnectorError(failure)
        return response.get("result")


def _light_or_switch_reason(
    entity: Mapping[str, object], kind: str, device_id: object
) -> str | None:
    if entity.get("platform") == "group":
        return GROUP_REASON
    if entity.get("disabled_by") is not None:
        return DISABLED_REASON
    # A switch outside a device, or one in a settings category, is a helper or an
    # integration option, not a plug or switch someone uses.
    if kind == "switch" and entity.get("entity_category") is not None:
        return SWITCH_CATEGORY_REASON
    if kind == "switch" and not device_id:
        return SWITCH_DEVICE_REASON
    return None


def _other_reason(  # noqa: PLR0911 -- one return per ordered check
    entity: Mapping[str, object], entity_id: str, device_id: object
) -> str | None:
    """Why an entity of any other domain is not a device to record, checked in this order."""
    domain = entity_id.partition(".")[0]
    if entity.get("disabled_by") is not None:
        return DISABLED_REASON
    if domain in PRESENCE_DOMAINS:
        return PRESENCE_REASON
    if entity.get("entity_category") is not None:
        return CATEGORY_REASON
    if entity.get("hidden_by") is not None:
        return HIDDEN_REASON
    if not device_id:
        return NO_DEVICE_REASON
    if entity.get("platform") == "group" or domain == "group":
        return GROUP_REASON
    return None


def _state_words(session: "Session") -> dict[tuple[str, str, str], str]:
    """Home Assistant's translated state words in its own language, else English. A failed
    translations read leaves no words, so every label falls back to the raw state.

    Unverified against a live Home Assistant: the result is read as `{"resources": {key:
    word}}`, with keys `component.<domain>.entity_component.<device class or _>.state.<state>`.
    """
    # The same failures `_session` turns into codes; here they only cost the language or the
    # words. An unknown language asks for English.
    failures = (ConnectorError, OSError, WebSocketException, ValueError)
    try:
        config = session.request("get_config", failure="discovery")
        language = config.get("language") if is_json_object(config) else None
    except failures:
        language = None
    try:
        result = session.request(
            "frontend/get_translations",
            failure="discovery",
            language=language
            if isinstance(language, str) and _LANGUAGE.fullmatch(language)
            else "en",
            category="entity_component",
        )
    except failures:
        return {}
    resources = result.get("resources") if is_json_object(result) else None
    if not is_json_object(resources):
        return {}
    words: dict[tuple[str, str, str], str] = {}
    for key, word in resources.items():
        match = _STATE_WORD.fullmatch(key)
        if match and isinstance(word, str) and 0 < len(word) <= MAX_STATE_LENGTH:
            domain, device_class, state = match.groups()
            words[domain, device_class, state] = word
    return words


def _rows(value: object) -> list[object]:
    """A raw list from Home Assistant; its callers validate each row."""
    if not isinstance(value, list):
        raise ConnectorError("invalid_data")
    return cast("list[object]", value)


def _timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("Invalid history timestamp")
    return datetime.fromtimestamp(value, UTC)


def history_state(row: object, source_entity_id: str) -> DeviceState:
    """One compressed history row, `{s, a, lc, lu}`, as a device's state. Times are Unix
    seconds; Home Assistant always sends `lu` and leaves out `lc` when it equals `lu`. Only
    the approved light attributes are kept; every other device keeps its state only."""
    if not is_json_object(row):
        raise ValueError("Invalid history row")
    updated = _timestamp(row.get("lu"))
    state = device_state(
        {
            "entity_id": source_entity_id,
            "state": row.get("s"),
            "attributes": row.get("a", {}),
            "last_changed": _timestamp(row["lc"]) if "lc" in row else updated,
            "last_updated": updated,
        },
        source_entity_id,
    )
    if state is None:
        raise ValueError("Invalid history row")
    return state
