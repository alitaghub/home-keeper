import math
from datetime import UTC, datetime, timedelta
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

# The command shape is shared with the controller and the demo reader.
from home_keeper_light_command import LightCommand, SwitchCommand

Identity = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
]
Name = Annotated[str, StringConstraints(min_length=1, max_length=256)]
# A name the command log stores. Postgres JSONB cannot hold a NUL character.
StoredName = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^[^\x00]*$")]
SourceId = Literal["demo", "home_assistant"]
# What a device is, from its Home Assistant domain. Separate from a record's `kind`.
# The kinds Home Keeper can switch.
SwitchableKind = Literal["light", "switch"]
# The kinds with on-time usage. `on_off`: a device that is only on or off, such as a fan or a
# door sensor, which Home Keeper never switches.
UsageKind = SwitchableKind | Literal["on_off"]
# `state`: any other device, shown by its state text alone. `measurement`: a sensor whose
# state class is `measurement`, a value Home Assistant keeps statistics for, such as a power
# meter.
DeviceKind = UsageKind | Literal["state", "measurement"]
# People and trackers report where someone is, which Home Keeper does not record.
PRESENCE_DOMAINS = frozenset({"person", "device_tracker"})
# Domains whose state is only `on` or `off`, apart from lights and switches.
ON_OFF_DOMAINS = frozenset({"fan", "binary_sensor", "siren", "humidifier", "remote"})
# Domains whose entities are measurements when their state class is MEASUREMENT_STATE_CLASS.
MEASUREMENT_DOMAINS = frozenset({"sensor"})
# The only state class Home Assistant keeps a mean, lowest and highest value for. A `total` or
# `total_increasing` sensor, such as an energy meter, has only a sum and stays `state`.
MEASUREMENT_STATE_CLASS = "measurement"


def device_kind_of(source_entity_id: str) -> DeviceKind | None:
    """The device kind a Home Assistant domain names, or None for presence and groups. A
    sensor is `state` here: only its state's `state_class`, which discovery reads, makes it a
    `measurement`."""
    match source_entity_id.partition(".")[0]:
        case "light":
            return "light"
        case "switch":
            return "switch"
        case domain if domain in ON_OFF_DOMAINS:
            return "on_off"
        case domain if domain in PRESENCE_DOMAINS or domain == "group":
            return None
        case _:
            return "state"


def device_kind_tag(value: object) -> object:
    """Pick a variant from its device kind. A stored record may omit a light's kind."""
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


# The most entities a catalog holds: one per entity registry row Home Assistant may list.
MAX_CATALOG_ENTITIES = 5000


class Catalog(WireModel):
    source_id: SourceId
    entities: list[Entity] = Field(max_length=MAX_CATALOG_ENTITIES)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({entity.id for entity in self.entities}) != len(self.entities):
            raise ValueError("Duplicate entity identity")
        return self


# The most devices one can monitor at once.
MAX_SELECTION = 500


class Selection(WireModel):
    """The devices the user monitors."""

    entity_ids: list[Identity] = Field(max_length=MAX_SELECTION)


class SelectionChange(WireModel):
    selected: bool


CHANNEL_MAX = 255
Channel = Annotated[int, Field(strict=True, ge=0, le=CHANNEL_MAX)]
ColorMode = Literal[
    "unknown", "onoff", "brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"
]
LightStateValue = Literal["on", "off", "unavailable", "unknown"]
Availability = Literal["available", "unavailable", "unknown"]
# Home Assistant caps a state at 255 characters; a translated state word is held to the same.
MAX_STATE_LENGTH = 255
StateText = Annotated[str, StringConstraints(max_length=MAX_STATE_LENGTH)]


def availability_of(state: str) -> Availability:
    """`unavailable` and `unknown` are Home Assistant's words for a state it cannot report."""
    if state == "unavailable":
        return "unavailable"
    if state == "unknown":
        return "unknown"
    return "available"


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
    # Home Assistant's translated word for the state; the connector sets it after parsing.
    label: StateText | None = None

    @model_validator(mode="after")
    def state_availability(self) -> Self:
        self.availability = _checked_availability(self.state, self.availability)
        return self

    @field_validator("last_changed", "last_updated", "last_reported", check_fields=False)
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_utc(value)


def _checked_availability(state: str, availability: Availability | None) -> Availability:
    expected = availability_of(state)
    if availability is not None and availability != expected:
        raise ValueError("Availability contradicts state")
    return expected


class LightState(StateHeader, LightAttributes):
    device_kind: Literal["light"] = "light"


class SwitchState(StateHeader):
    """A plug or switch: on or off, with no light attributes."""

    device_kind: Literal["switch"]


class OnOffState(StateHeader):
    """Any other device that is only on or off, such as a fan or a door sensor."""

    device_kind: Literal["on_off"]


class TextStateHeader(WireModel):
    """A state text and the source times, with no attributes."""

    state: StateText
    last_changed: datetime | None = None
    last_updated: datetime | None = None
    last_reported: datetime | None = None
    availability: Availability | None = None
    # Home Assistant's translated word for the state; the connector sets it after parsing.
    label: StateText | None = None

    @model_validator(mode="after")
    def state_availability(self) -> Self:
        self.availability = _checked_availability(self.state, self.availability)
        return self

    @field_validator("last_changed", "last_updated", "last_reported")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_utc(value)


class GenericState(TextStateHeader):
    """Any other device: its state text and source times, with no attributes."""

    device_kind: Literal["state"]


# Home Assistant's unit of measurement, such as `W` or `°C`.
MAX_UNIT_LENGTH = 32
Unit = Annotated[str, StringConstraints(min_length=1, max_length=MAX_UNIT_LENGTH)]
# A measured number. JSON like 1e999 parses to infinity, which no measurement is.
Measured = Annotated[float, Field(allow_inf_nan=False)]


def measured_value(state: str) -> float | None:
    """A measurement's state as a finite number; None for any other text, such as
    `unavailable`."""
    try:
        value = float(state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


class MeasurementState(TextStateHeader):
    """A sensor with the state class `measurement`: its state text, the number it holds and
    its unit."""

    device_kind: Literal["measurement"]
    # Always the state's own number; set from the state, never from the input.
    value: Measured | None = None
    unit: Unit | None = None

    @model_validator(mode="after")
    def state_value(self) -> Self:
        self.value = measured_value(self.state)
        return self


# A state as the connector parses it from Home Assistant.
DeviceState = LightState | SwitchState | OnOffState | GenericState | MeasurementState


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


# Public API responses. Records are stored as JSON and returned as stored, so timestamps
# stay the exact ISO strings the API wrote instead of being re-serialized as datetimes.
Timestamp = Annotated[str, Field(json_schema_extra={"format": "date-time"})]
# Why Home Assistant could not be read: its own failure, or the reader service itself.
ReaderError = CollectorError | Literal["reader_unreachable"]


class CatalogResponse(WireModel):
    source_id: SourceId
    # Browser-facing Home Assistant origin, so the dashboard can link to it. None in the demo.
    home_assistant_url: str | None
    entities: list[Entity]


class Unreachable(WireModel):
    """The 503 body of a route that needs Home Assistant while it cannot be read."""

    error: ReaderError
    # Fixed text for the error code; never upstream text, URLs or credentials.
    message: str


class ReadingHeader(WireModel):
    state: LightStateValue
    availability: Availability
    last_changed: Timestamp | None = None
    last_updated: Timestamp | None = None
    last_reported: Timestamp | None = None
    # Home Assistant's translated word for the state, else the raw state.
    label: StateText


# Pydantic orders fields from the last base to the first, so the header fields lead the body.
class LightReading(LightAttributes, ReadingHeader):
    """Light attributes as stored. Event states omit unset attributes, so routes returning
    readings exclude unset fields to keep absent keys absent."""

    device_kind: Literal["light"]


class SwitchReading(ReadingHeader):
    """A plug or switch state as stored: no light attributes."""

    device_kind: Literal["switch"]


class OnOffReading(ReadingHeader):
    """Any other on/off device's state as stored, such as a fan or a door sensor: no
    attributes."""

    device_kind: Literal["on_off"]


class TextReadingHeader(WireModel):
    state: StateText
    availability: Availability
    last_changed: Timestamp | None = None
    last_updated: Timestamp | None = None
    last_reported: Timestamp | None = None
    # Home Assistant's translated word for the state, else the raw state.
    label: StateText


class StateReading(TextReadingHeader):
    """Any other device's state as Home Assistant reports it: its text, with no attributes."""

    device_kind: Literal["state"]


class MeasurementReading(TextReadingHeader):
    """A measurement's state as Home Assistant reports it: its text, its number and unit."""

    device_kind: Literal["measurement"]
    # The state as a finite number; null when it is not one, such as `unavailable`.
    value: Measured | None
    # Home Assistant's `unit_of_measurement`; null when the device has none.
    unit: Unit | None


Reading = Annotated[
    Annotated[LightReading, Tag("light")]
    | Annotated[SwitchReading, Tag("switch")]
    | Annotated[OnOffReading, Tag("on_off")]
    | Annotated[StateReading, Tag("state")]
    | Annotated[MeasurementReading, Tag("measurement")],
    Discriminator(device_kind_tag),
]


class ChangeRecord(WireModel):
    """A change Home Assistant recorded for a monitored device: two consecutive history rows
    whose state, availability, brightness or color differ."""

    # `{entity_id}@{time}`.
    id: str
    entity_id: Identity
    source_entity_id: Identity
    device_kind: DeviceKind
    # Home Assistant's last_updated of the new state.
    time: Timestamp
    old: Reading
    new: Reading


class EventsPage(WireModel):
    events: list[ChangeRecord]
    # Opaque; it carries the window of the first page.
    next_cursor: str | None


class HealthStatus(WireModel):
    """Whether Home Assistant answers through the reader, checked at request time."""

    source_id: SourceId
    status: Literal["healthy", "unreachable"]
    error: ReaderError | None
    checked_at: Timestamp


class MonitoredEntity(Entity):
    # Home Assistant's entity ID, such as light.kitchen; None while it cannot be read.
    source_entity_id: Identity | None
    # What Home Assistant reports now; None when it cannot be read or has no such entity.
    state: Reading | None
    # Home Assistant no longer has the entity.
    missing: bool


class MonitoredEntities(WireModel):
    entities: list[MonitoredEntity]


# no_history: Home Assistant has no row for the time. unavailable: it reported the device
# `unavailable` or `unknown`.
UnknownReason = Literal["no_history", "unavailable"]


class UnknownInterval(WireModel):
    start: Timestamp
    end: Timestamp
    reason: UnknownReason


class SwitchPeriod(WireModel):
    """A stretch the evidence shows on or off. Periods and unknown intervals tile the range."""

    start: Timestamp
    end: Timestamp
    state: Literal["on", "off"]


class LightPeriod(SwitchPeriod):
    # 0-255 while on, when Home Assistant reported it; always null while off.
    brightness: int | None


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
    periods: list[LightPeriod]


class SwitchUsage(UsageFields):
    """A plug or switch: the same numbers as a light, with no brightness."""

    device_kind: Literal["switch"]
    periods: list[SwitchPeriod]


class OnOffUsage(UsageFields):
    """Any other on/off device, such as a fan or a door sensor: a plug's numbers, with Home
    Assistant's words for its on and off."""

    device_kind: Literal["on_off"]
    periods: list[SwitchPeriod]
    # Home Assistant's translated words for `on` and `off`, such as "Open" and "Closed" for a
    # door, else the raw state.
    on_label: StateText
    off_label: StateText


DeviceUsage = Annotated[LightUsage | SwitchUsage | OnOffUsage, Field(discriminator="device_kind")]


class UsageReport(WireModel):
    start: Timestamp
    end: Timestamp
    timezone: str
    devices: list[DeviceUsage]


# Home Assistant's statistics periods Home Keeper reads: 5-minute short-term statistics, or
# hourly long-term ones.
StatisticsPeriod = Literal["5minute", "hour"]
# The longest range read in 5-minute statistics; a longer one is read hourly.
SHORT_TERM_RANGE = timedelta(hours=24)


def statistics_period(start: datetime, end: datetime) -> StatisticsPeriod:
    """The statistics period for a range: 5-minute buckets up to 24 hours, hourly above."""
    return "5minute" if end - start <= SHORT_TERM_RANGE else "hour"


class Bucket(WireModel):
    """One period of Home Assistant's statistics for a measurement: from `start` to `end`,
    the mean, the lowest and the highest value."""

    start: Timestamp
    end: Timestamp
    mean: Measured
    min: Measured
    max: Measured

    @model_validator(mode="after")
    def ordered_times(self) -> Self:
        if _aware_time(self.start) >= _aware_time(self.end):
            raise ValueError("A bucket must end after it starts")
        return self


def _aware_time(value: str) -> datetime:
    """Raises ValueError for a malformed time or one without a timezone."""
    return to_utc(datetime.fromisoformat(value))


class MeasurementSeries(WireModel):
    """One measurement's statistics over a report's range. Periods Home Assistant has no
    statistics for have no bucket: they are gaps, not zeros."""

    entity_id: Identity
    # From the current reading; null when the device has none or cannot be read.
    unit: Unit | None
    period: StatisticsPeriod
    buckets: list[Bucket]
    # The lowest bucket minimum, the mean of the bucket means and the highest bucket maximum;
    # null when there is no bucket.
    min: float | None
    mean: float | None
    max: float | None
    # The current reading's value; null when it is not a number or cannot be read.
    latest: float | None


class MeasurementReport(WireModel):
    start: Timestamp
    end: Timestamp
    devices: list[MeasurementSeries]


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
    name: StoredName


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


class LightCommandRecord(WireModel):
    id: str
    entity_id: Identity
    device_kind: Literal["light"]
    command: LightCommand
    sender: Sender
    sent_at: Timestamp
    outcome: CommandOutcome


class SwitchCommandRecord(WireModel):
    id: str
    entity_id: Identity
    device_kind: Literal["switch"]
    command: SwitchCommand
    sender: Sender
    sent_at: Timestamp
    outcome: CommandOutcome


CommandRecord = Annotated[
    Annotated[LightCommandRecord, Tag("light")] | Annotated[SwitchCommandRecord, Tag("switch")],
    Discriminator(device_kind_tag),
]


# Why the API refused a command before sending it.
# `not_supported`: the device cannot do what the command asks, such as a brightness for a plug,
# or cannot be switched at all.
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
