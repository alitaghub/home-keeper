"""Trusted, fixed-read Home Assistant adapter. Raw registry data never leaves it."""

import ipaddress
import json
import logging
import re
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from home_observer.events import COLOR_FIELDS, change_event, light_state
from home_observer.models import Catalog, Entity, Observation

MAX_PAYLOAD = 2 * 1024 * 1024
_REGISTRY_ID = re.compile(r"^[a-zA-Z0-9_-]{1,120}$")
_LIGHT_ID = re.compile(r"^light\.[a-z0-9_]{1,120}$")
_WS_LOGGER = logging.getLogger("home_observer.transport.private")
_WS_LOGGER.addHandler(logging.NullHandler())
_WS_LOGGER.propagate = False
_WS_LOGGER.setLevel(logging.CRITICAL + 1)


class ConnectorError(Exception):
    """Contains a fixed safe code, never upstream exception or response text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Home Assistant collection failed ({code})")


class Connector(Protocol):
    def discover(self) -> Catalog: ...
    def approve(self, selected: dict[str, int]) -> None: ...
    def read_selected(self, identity: str, revision: int) -> Observation: ...
    def subscribe_selected(self): ...


class HomeAssistantConnector:
    def __init__(
        self,
        origin: str,
        token_file: Path,
        timeout_seconds: float = 5,
        redaction_values: tuple[str, ...] = (),
    ):
        try:
            if any(not 33 <= ord(character) <= 126 for character in origin):
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
                or not 0 < timeout_seconds <= 30
            ):
                raise ValueError
            parsed.port
            if ":" in parsed.hostname:
                ipaddress.ip_address(parsed.hostname)
            elif not re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname):
                raise ValueError
            token = token_file.read_text().strip()
            if (
                not token
                or len(token) > 4096
                or not token.isascii()
                or any(not 33 <= ord(character) <= 126 for character in token)
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

    def close(self):
        session = getattr(self, "_collection_session", None)
        if session is not None:
            session.close()
        self._http.close()

    def discover(self) -> Catalog:
        # A failed refresh forbids reading even cached identities until revalidated.
        self._validated = False
        self._approved = {}
        try:
            with connect(
                self._websocket_url,
                open_timeout=self._timeout,
                close_timeout=self._timeout,
                max_size=MAX_PAYLOAD,
                proxy=None,
                logger=_WS_LOGGER,
            ) as socket:
                if self._receive(socket).get("type") != "auth_required":
                    raise ConnectorError("invalid_data")
                socket.send(json.dumps({"type": "auth", "access_token": self._token}))
                authentication = self._receive(socket).get("type")
                if authentication == "auth_invalid":
                    raise ConnectorError("authentication")
                if authentication != "auth_ok":
                    raise ConnectorError("invalid_data")
                entities = self._registry(socket, 1, "config/entity_registry/list")
                devices = self._registry(socket, 2, "config/device_registry/list")
            catalog, mapping = self._project(entities, devices)
        except ConnectorError:
            raise
        except ssl.SSLError:
            raise ConnectorError("tls") from None
        except TimeoutError:
            raise ConnectorError("timeout") from None
        except (OSError, WebSocketException):
            raise ConnectorError("connection") from None
        except (ValueError, TypeError, KeyError):
            raise ConnectorError("invalid_data") from None
        self._entities = mapping
        self._supported = {entity.id for entity in catalog.entities if entity.supported}
        self._validated = True
        return catalog

    def _receive(self, socket) -> dict:
        raw = socket.recv(timeout=self._timeout)
        if len(raw) > MAX_PAYLOAD:
            raise ConnectorError("invalid_data")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ConnectorError("invalid_data")
        return data

    def _registry(self, socket, request_id: int, command: str) -> list:
        # This private helper is also guarded: future callers cannot add write commands.
        if command not in {"config/entity_registry/list", "config/device_registry/list"}:
            raise ConnectorError("configuration")
        socket.send(json.dumps({"id": request_id, "type": command}))
        response = self._receive(socket)
        if response.get("type") != "result" or response.get("id") != request_id:
            raise ConnectorError("invalid_data")
        if response.get("success") is not True:
            raise ConnectorError("discovery")
        result = response.get("result")
        if not isinstance(result, list):
            raise ConnectorError("invalid_data")
        return result

    def _name(self, value, fallback: str) -> str:
        name = value if isinstance(value, str) and value else fallback
        for secret in self._redactions:
            if secret:
                name = name.replace(secret, "[redacted]")
        return name[:256]

    def _project(self, entities: list, devices: list) -> tuple[Catalog, dict[str, str]]:
        if len(entities) > 500 or len(devices) > 500:
            raise ConnectorError("invalid_data")
        device_names = {}
        for device in devices:
            identity = device.get("id") if isinstance(device, dict) else None
            if not isinstance(identity, str) or not _REGISTRY_ID.fullmatch(identity):
                raise ConnectorError("invalid_data")
            device_names[identity] = self._name(
                device.get("name_by_user") or device.get("name"), "Unnamed device"
            )
        projected = []
        mapping = {}
        for entity in entities:
            if not isinstance(entity, dict):
                raise ConnectorError("invalid_data")
            registry_id = entity.get("id")
            entity_id = entity.get("entity_id")
            if (
                not isinstance(registry_id, str)
                or not _REGISTRY_ID.fullmatch(registry_id)
                or not isinstance(entity_id, str)
                or len(entity_id) > 128
            ):
                raise ConnectorError("invalid_data")
            identity = f"ha:{registry_id}"
            device_id = entity.get("device_id")
            if device_id is not None and device_id not in device_names:
                # Dangling relationships retain the entity without inventing a device.
                device_id = None
            supported = (
                bool(_LIGHT_ID.fullmatch(entity_id))
                and entity.get("platform") == "hue"
                and entity.get("translation_key") == "hue_light"
                and entity.get("disabled_by") is None
            )
            projected.append(
                Entity(
                    id=identity,
                    name=self._name(
                        entity.get("name") or entity.get("original_name"), "Unnamed entity"
                    ),
                    device_id=f"ha:{device_id}" if device_id else None,
                    device_name=device_names.get(device_id),
                    supported=supported,
                    reason=None if supported else "Only enabled individual Hue V2 lights supported",
                )
            )
            mapping[identity] = entity_id
        return Catalog(source_id="home_assistant", entities=projected), mapping

    def approve(self, selected: dict[str, int]) -> None:
        if not self._validated or any(
            identity not in self._supported or not isinstance(revision, int) or revision < 0
            for identity, revision in selected.items()
        ):
            raise ConnectorError("configuration")
        self._approved = dict(selected)

    @property
    def subscription_key(self):
        if not self._validated:
            return ()
        return tuple(
            sorted(
                (identity, revision, self._entities[identity])
                for identity, revision in self._approved.items()
            )
        )

    def subscribe_selected(self):
        if not self.subscription_key:
            return None
        return SelectedSubscription(self)

    def read_selected(self, identity: str, revision: int) -> Observation:
        if (
            not self._validated
            or identity not in self._supported
            or self._approved.get(identity) != revision
        ):
            raise ConnectorError("configuration")
        entity_id = self._entities[identity]
        deadline = time.monotonic() + self._timeout
        try:
            with self._http.stream("GET", f"/api/states/{entity_id}") as response:
                if response.status_code in {401, 403}:
                    raise ConnectorError("authentication")
                if response.status_code == 404:
                    raise ConnectorError("missing")
                if response.status_code != 200:
                    raise ConnectorError("connection")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise ConnectorError("timeout")
                    payload.extend(chunk)
                    if len(payload) > MAX_PAYLOAD:
                        raise ConnectorError("invalid_data")
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("entity_id") != entity_id:
                raise ConnectorError("invalid_data")
            state = data.get("state")
            if state not in {"on", "off", "unavailable", "unknown"}:
                raise ConnectorError("invalid_data")
            attributes = data.get("attributes", {})
            if not isinstance(attributes, dict):
                raise ConnectorError("invalid_data")
            return Observation(
                entity_id=identity,
                source_entity_id=entity_id,
                source_id="home_assistant",
                selection_revision=revision,
                state=state,
                availability=(state if state in {"unknown", "unavailable"} else "available"),
                received_at=datetime.now(UTC),
                **{key: data.get(key) for key in ("last_changed", "last_updated", "last_reported")},
                **{key: attributes.get(key) for key in COLOR_FIELDS},
            )
        except ConnectorError:
            raise
        except httpx.TimeoutException:
            raise ConnectorError("timeout") from None
        except httpx.HTTPError as error:
            # httpx wraps TLS errors; inspect types, never stringify secrets-bearing causes.
            cause = error
            while cause is not None:
                if isinstance(cause, ssl.SSLError):
                    raise ConnectorError("tls") from None
                cause = cause.__cause__
            raise ConnectorError("connection") from None
        except (ValueError, TypeError, ValidationError):
            raise ConnectorError("invalid_data") from None


class SelectedSubscription:
    """Only fixed state_changed subscriptions, with filtering before projection."""

    def __init__(self, connector: HomeAssistantConnector):
        self._connector = connector
        self.key = connector.subscription_key
        if not self.key:
            raise ConnectorError("configuration")
        self._mapping = {source: (identity, revision) for identity, revision, source in self.key}
        self._socket = None
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
            self._raise_safe(error)

    @staticmethod
    def _raise_safe(error):
        if isinstance(error, ConnectorError):
            raise error
        if isinstance(error, ssl.SSLError):
            raise ConnectorError("tls") from None
        if isinstance(error, TimeoutError):
            raise ConnectorError("timeout") from None
        if isinstance(error, (OSError, WebSocketException)):
            raise ConnectorError("connection") from None
        raise ConnectorError("invalid_data") from None

    def receive(self, timeout: float = 1):
        if not 0 < timeout <= 5:
            raise ConnectorError("configuration")
        # Approvals are refreshed by the collector. Do not drain old permissions.
        if self._connector._validated and self.key != self._connector.subscription_key:
            return None
        try:
            try:
                raw = self._socket.recv(timeout=timeout)
            except TimeoutError:
                return None
            if len(raw) > MAX_PAYLOAD:
                raise ConnectorError("invalid_data")
            message = json.loads(raw)
            if (
                not isinstance(message, dict)
                or message.get("type") != "event"
                or message.get("id") != 1
            ):
                raise ConnectorError("invalid_data")
            envelope = message.get("event")
            if not isinstance(envelope, dict) or envelope.get("event_type") != "state_changed":
                raise ConnectorError("invalid_data")
            data = envelope.get("data")
            if not isinstance(data, dict):
                raise ConnectorError("invalid_data")
            source_entity_id = data.get("entity_id")
            if not isinstance(source_entity_id, str) or source_entity_id not in self._mapping:
                return None
            identity, revision = self._mapping[source_entity_id]
            old = light_state(data.get("old_state"), source_entity_id)
            new = light_state(data.get("new_state"), source_entity_id)
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
            self._raise_safe(error)

    def close(self):
        if self._socket is not None:
            try:
                self._socket.close()
            except (OSError, WebSocketException):
                pass
            self._socket = None
