from datetime import UTC, datetime
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    ValidationInfo,
    field_validator,
    model_validator,
)

# The command shape is shared with the controller and the demo collector.
from home_keeper_light_command import LightCommand

Identity = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
]
Name = Annotated[str, StringConstraints(min_length=1, max_length=256)]
SourceId = Literal["demo", "home_assistant"]
# What a device is, from its Home Assistant domain. Separate from a record's `kind`.
DeviceKind = Literal["light", "switch"]


def device_kind_of(source_entity_id: str) -> DeviceKind | None:
    """The device kind a Home Assistant domain names, or None for every other domain."""
    match source_entity_id.partition(".")[0]:
        case "light":
            return "light"
        case "switch":
            return "switch"
        case _:
            return None


def light_by_default(data: object) -> object:
    """Records stored before plugs carry no device kind; every one of them is a light's."""
    if not isinstance(data, dict):
        return data
    record = cast(dict[str, object], data)
    return record if "device_kind" in record else record | {"device_kind": "light"}


def device_kind_tag(value: object) -> object:
    """Pick a reading's variant from its device kind, a light when it has none."""
    if isinstance(value, dict):
        return cast(dict[str, object], value).get("device_kind", "light")
    return getattr(value, "device_kind", "light")


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp requires a timezone")
    return value.astimezone(UTC)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(WireModel):
    id: Identity
    device_id: Identity | None = None
    device_name: Name | None = None
    name: Name
    # None for a domain Home Keeper does not record, such as a sensor.
    device_kind: DeviceKind | None = None
    supported: bool
    reason: Name | None = None

    @model_validator(mode="after")
    def unsupported_reason(self) -> Self:
        if not self.supported and not self.reason:
            raise ValueError("Unsupported entities need a reason")
        return self


class Catalog(WireModel):
    source_id: SourceId
    entities: list[Entity] = Field(max_length=500)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({entity.id for entity in self.entities}) != len(self.entities):
            raise ValueError("Duplicate entity identity")
        return self


class SelectionSnapshot(WireModel):
    revision: int = Field(strict=True, ge=0)
    refresh_revision: int = Field(default=0, strict=True, ge=0)
    entity_ids: list[Identity] = Field(max_length=500)


class SelectionChange(WireModel):
    selected: bool


CHANNEL_MAX = 255
Channel = Annotated[int, Field(strict=True, ge=0, le=CHANNEL_MAX)]
ColorMode = Literal[
    "unknown", "onoff", "brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"
]
LightStateValue = Literal["on", "off", "unavailable", "unknown"]
Availability = Literal["available", "unavailable", "unknown"]
Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class LightAttributes(WireModel):
    """Light attributes shared by incoming states and stored readings."""

    brightness: int | None = Field(default=None, strict=True, ge=0, le=255)
    color_mode: ColorMode | None = None
    color_temp_kelvin: int | None = Field(default=None, strict=True, ge=1000, le=10000)
    hs_color: tuple[Coordinate, Coordinate] | None = None
    xy_color: tuple[Coordinate, Coordinate] | None = None
    rgb_color: tuple[Channel, Channel, Channel] | None = None
    rgbw_color: tuple[Channel, Channel, Channel, Channel] | None = None
    rgbww_color: tuple[Channel, Channel, Channel, Channel, Channel] | None = None

    @field_validator("rgb_color", "rgbw_color", "rgbww_color")
    @classmethod
    def rgb_range(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        if value is not None and any(channel < 0 or channel > CHANNEL_MAX for channel in value):
            raise ValueError("RGB channels must be between 0 and 255")
        return value

    @field_validator("hs_color", "xy_color")
    @classmethod
    def coordinate_range(
        cls, value: tuple[float, float] | None, info: ValidationInfo
    ) -> tuple[float, float] | None:
        if value is not None:
            bounds = (360, 100) if info.field_name == "hs_color" else (1, 1)
            if any(not 0 <= channel <= bound for channel, bound in zip(value, bounds, strict=True)):
                raise ValueError("Color coordinate outside supported range")
        return value


class StateHeader(WireModel):
    """The on/off state and source times every device reports."""

    state: LightStateValue
    last_changed: datetime | None = None
    last_updated: datetime | None = None
    last_reported: datetime | None = None
    availability: Availability | None = None

    @model_validator(mode="after")
    def state_availability(self) -> Self:
        expected = "available" if self.state in ("on", "off") else self.state
        if self.availability is not None and self.availability != expected:
            raise ValueError("Availability contradicts state")
        self.availability = expected
        return self

    @field_validator("last_changed", "last_updated", "last_reported", check_fields=False)
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_utc(value)

    def meaningful(self) -> dict[str, object]:
        return self.model_dump(
            exclude={"last_changed", "last_updated", "last_reported"}, exclude_none=True
        )


class LightState(LightAttributes, StateHeader):
    device_kind: Literal["light"] = "light"


class SwitchState(StateHeader):
    """A plug or switch: on or off, with no light attributes."""

    device_kind: Literal["switch"]


DeviceState = Annotated[
    Annotated[LightState, Tag("light")] | Annotated[SwitchState, Tag("switch")],
    Discriminator(device_kind_tag),
]


class ObservationFields(WireModel):
    source_entity_id: Identity | None = None
    entity_id: Identity
    selection_revision: int = Field(ge=0)
    source_id: SourceId
    kind: Literal["baseline", "reconciliation", "recovery"] = "baseline"
    received_at: datetime
    collection_started_at: datetime | None = None

    @field_validator("received_at", "collection_started_at")
    @classmethod
    def receipt_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_utc(value)


# Availability is explicit on the existing snapshot protocol. Pydantic allows a subclass
# to make an optional field required; the checker cannot model that narrowing.
class Observation(LightState, ObservationFields):
    """A light snapshot."""

    availability: Literal["available", "unavailable", "unknown"]  # pyright: ignore[reportIncompatibleVariableOverride, reportGeneralTypeIssues]


class SwitchObservation(SwitchState, ObservationFields):
    """A plug or switch snapshot."""

    availability: Literal["available", "unavailable", "unknown"]  # pyright: ignore[reportIncompatibleVariableOverride, reportGeneralTypeIssues]


DeviceObservation = Annotated[
    Annotated[Observation, Tag("light")] | Annotated[SwitchObservation, Tag("switch")],
    Discriminator(device_kind_tag),
]


class ChangeEvent(WireModel):
    id: Identity
    entity_id: Identity
    source_entity_id: Identity | None = None
    source_id: SourceId
    selection_revision: int = Field(ge=0)
    event_time: datetime
    received_at: datetime
    old: DeviceState | None = None
    new: DeviceState | None = None

    @field_validator("event_time", "received_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_utc(value)

    @model_validator(mode="after")
    def has_state(self) -> Self:
        if self.old is None and self.new is None:
            raise ValueError("An event needs an old or new state")
        if self.old and self.new and self.old.device_kind != self.new.device_kind:
            raise ValueError("An event's states must be of one device kind")
        return self

    @property
    def device_kind(self) -> DeviceKind:
        state = self.new or self.old
        # has_state guarantees one of them.
        return state.device_kind if state is not None else "light"


CollectorError = Literal[
    "authentication",
    "connection",
    "tls",
    "timeout",
    "invalid_data",
    "missing",
    "discovery",
    "configuration",
]


class Health(WireModel):
    source_id: SourceId
    status: Literal["healthy", "failed"]
    error: CollectorError | None = None
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_utc(value)


GapReason = Literal[
    "connection",
    "ingestion",
    "authentication",
    "timeout",
    "restart",
    "overflow",
    "reconciliation",
    "heartbeat",
    "selection",
    "invalid_data",
    "configuration",
    "tls",
    "missing",
]


class Coverage(WireModel):
    source_id: SourceId
    status: Literal["interrupted", "recovered"]
    reason: GapReason = "connection"
    received_at: datetime
    entity_id: Identity | None = None
    selection_revision: int | None = Field(default=None, ge=0)
    entity_ids: list[Identity] | None = Field(default=None, max_length=500)

    @field_validator("received_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_utc(value)


# Public API responses. Records are stored as JSON and returned as stored, so timestamps
# stay the exact ISO strings the API wrote instead of being re-serialized as datetimes.
Timestamp = Annotated[str, Field(json_schema_extra={"format": "date-time"})]
Freshness = Literal["pending", "fresh", "stale"]


class CatalogResponse(WireModel):
    source_id: SourceId
    # Browser-facing Home Assistant origin, so the dashboard can link to it. None in the demo.
    home_assistant_url: str | None
    refreshed_at: Timestamp | None
    freshness: Freshness
    entities: list[Entity]


class DiscoveryRefresh(WireModel):
    refresh_revision: int


class ReadingHeader(WireModel):
    state: LightStateValue
    availability: Availability
    last_changed: Timestamp | None = None
    last_updated: Timestamp | None = None
    last_reported: Timestamp | None = None


# Pydantic orders fields from the last base to the first, so the header fields lead the body.
class LightReading(LightAttributes, ReadingHeader):
    """Light attributes as stored. Event states omit unset attributes, so routes returning
    readings exclude unset fields to keep absent keys absent."""

    device_kind: Literal["light"]

    @model_validator(mode="before")
    @classmethod
    def stored_before_plugs(cls, data: object) -> object:
        # Setting the kind here, not as a default, keeps it in responses that exclude unset
        # fields.
        return light_by_default(data)


class SwitchReading(ReadingHeader):
    """A plug or switch state as stored: no light attributes."""

    device_kind: Literal["switch"]


Reading = Annotated[
    Annotated[LightReading, Tag("light")] | Annotated[SwitchReading, Tag("switch")],
    Discriminator(device_kind_tag),
]


class BaselineFields(WireModel):
    id: str
    kind: Literal["baseline", "reconciliation", "recovery"]
    entity_id: Identity
    source_entity_id: Identity | None
    source_id: SourceId
    received_at: Timestamp
    collection_started_at: Timestamp | None
    persisted_at: Timestamp
    # Present only when the snapshot closes a coverage gap.
    coverage_gap_ids: list[str] | None = None
    transition_time: Literal["unknown"] | None = None


class LightBaseline(BaselineFields, LightReading):
    pass


class SwitchBaseline(BaselineFields, SwitchReading):
    pass


BaselineRecord = Annotated[
    Annotated[LightBaseline, Tag("light")] | Annotated[SwitchBaseline, Tag("switch")],
    Discriminator(device_kind_tag),
]


class EventFields(WireModel):
    """The latest state taken from a recorded change event."""

    id: Identity
    kind: Literal["event"]
    entity_id: Identity
    source_entity_id: Identity | None
    source_id: SourceId
    event_time: Timestamp
    received_at: Timestamp
    persisted_at: Timestamp


class LightEventReading(EventFields, LightReading):
    pass


class SwitchEventReading(EventFields, SwitchReading):
    pass


EventReading = Annotated[
    Annotated[LightEventReading, Tag("light")] | Annotated[SwitchEventReading, Tag("switch")],
    Discriminator(device_kind_tag),
]


def record_kind_tag(value: object) -> object:
    """Pick a current reading's variant: a recorded change event or a snapshot."""
    if isinstance(value, dict):
        kind = cast(dict[str, object], value).get("kind")
    else:
        kind = getattr(value, "kind", None)
    return "event" if kind == "event" else "baseline"


# The newest state of a monitored device: a snapshot or a recorded change event. A callable
# discriminator, because a `kind` tag would map to two device kinds, which OpenAPI forbids.
CurrentReading = Annotated[
    Annotated[BaselineRecord, Tag("baseline")] | Annotated[EventReading, Tag("event")],
    Discriminator(record_kind_tag),
]


class BaselinesPage(WireModel):
    baselines: list[BaselineRecord]
    next_cursor: int | None


class EventRecord(WireModel):
    id: Identity
    entity_id: Identity
    source_entity_id: Identity | None
    source_id: SourceId
    selection_revision: int
    event_time: Timestamp
    received_at: Timestamp
    persisted_at: Timestamp
    device_kind: DeviceKind
    old: Reading | None
    new: Reading | None

    @model_validator(mode="before")
    @classmethod
    def stored_before_plugs(cls, data: object) -> object:
        return light_by_default(data)


class EventsPage(WireModel):
    events: list[EventRecord]
    next_cursor: str | None
    retained_since: Timestamp
    coverage: Literal["unknown"]


class CoverageGap(WireModel):
    id: str
    source_id: SourceId
    entity_ids: list[Identity]
    last_confirmed_at: Timestamp | None
    uncertain_start: bool
    detected_at: Timestamp
    recovered_at: Timestamp | None
    reason: GapReason


class CoverageReport(WireModel):
    gaps: list[CoverageGap]
    state: Literal["interrupted", "collecting", "unknown"]
    last_confirmed_at: Timestamp | None
    retained_since: Timestamp
    continuous_coverage: Literal[False]
    outside_recorded_windows: Literal["unknown"]


class HealthPending(WireModel):
    status: Literal["pending"]
    source_id: SourceId


class HealthReport(WireModel):
    status: Literal["healthy", "failed", "stale"]
    source_id: SourceId
    error: CollectorError | None
    received_at: Timestamp
    persisted_at: Timestamp


HealthStatus = Annotated[HealthPending | HealthReport, Field(discriminator="status")]


class MonitoredEntity(Entity):
    baseline: CurrentReading | None
    freshness: Freshness
    confirmed_at: Timestamp | None


class MonitoredEntities(WireModel):
    entities: list[MonitoredEntity]


UnknownReason = Literal["gap", "no_evidence", "unavailable"]


class UnknownInterval(WireModel):
    start: Timestamp
    end: Timestamp
    reason: UnknownReason


class DayUsage(WireModel):
    # Local calendar date, YYYY-MM-DD, in the report's time zone.
    date: str
    on_seconds: float
    unknown_seconds: float


class UsageFields(WireModel):
    entity_id: Identity
    on_seconds: float
    unknown_seconds: float
    switch_ons: int
    days: list[DayUsage]
    # On-seconds per local hour of day, 0 to 23, summed over the whole range.
    hours: list[float] = Field(min_length=24, max_length=24)
    unknown_intervals: list[UnknownInterval]


class LightUsage(UsageFields):
    device_kind: Literal["light"]
    average_brightness: float | None


class SwitchUsage(UsageFields):
    """A plug or switch: the same numbers as a light, with no brightness."""

    device_kind: Literal["switch"]


DeviceUsage = Annotated[LightUsage | SwitchUsage, Field(discriminator="device_kind")]


class UsageReport(WireModel):
    start: Timestamp
    end: Timestamp
    timezone: str
    retained_since: Timestamp
    devices: list[DeviceUsage]


class ControlHeartbeat(WireModel):
    """The controller's proof that it is running and can be sent commands."""

    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return to_utc(value)


class ControlStatus(WireModel):
    # `on` while the controller's latest heartbeat is fresh.
    state: Literal["on", "off"]
    heartbeat_at: Timestamp | None


class DashboardSender(WireModel):
    kind: Literal["dashboard"]


class BrowserChatSender(WireModel):
    kind: Literal["browser_chat"]


class TelegramSender(WireModel):
    kind: Literal["telegram"]
    id: int = Field(strict=True, gt=0)
    # The display name at send time. Untrusted text, like device names.
    name: Name


# Who asked for a command. A label the caller reports for the audit, not authentication.
Sender = Annotated[
    DashboardSender | BrowserChatSender | TelegramSender, Field(discriminator="kind")
]


class CommandRequest(WireModel):
    command: LightCommand
    sender: Sender


# What Home Keeper saw after it forwarded a command. `pending` while it waits;
# `interrupted` when the API restarted before the outcome was known.
CommandOutcome = Literal[
    "pending", "observed", "not_observed", "refused", "unreachable", "interrupted"
]


class CommandRecord(WireModel):
    id: str
    entity_id: Identity
    device_kind: DeviceKind
    # A switch's command never carries a brightness: the API refuses one before sending.
    command: LightCommand
    sender: Sender
    sent_at: Timestamp
    outcome: CommandOutcome
    # The recorded event that showed the change, when an event (not a snapshot) showed it.
    event_id: Identity | None

    @model_validator(mode="before")
    @classmethod
    def stored_before_plugs(cls, data: object) -> object:
        return light_by_default(data)


# Why the API refused a command before sending it.
# `not_supported`: the device cannot do what the command asks, such as a brightness for a plug.
CommandRefusalCode = Literal[
    "control_off",
    "not_monitored",
    "state_unknown",
    "unavailable",
    "already_in_state",
    "busy",
    "not_supported",
]


class CommandRefusal(WireModel):
    code: CommandRefusalCode
    message: str


class CommandRefused(WireModel):
    """The 409 body of a refused command."""

    detail: CommandRefusal


class CommandsPage(WireModel):
    commands: list[CommandRecord]
    next_cursor: str | None
    retained_since: Timestamp
