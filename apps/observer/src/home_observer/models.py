from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

Identity = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
]
Name = Annotated[str, StringConstraints(min_length=1, max_length=256)]
SourceId = Literal["demo", "home_assistant"]


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


class LightState(LightAttributes):
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


class Observation(LightState):
    # Availability is explicit on the existing snapshot protocol. Pydantic allows a subclass
    # to make an optional field required; the checker cannot model that narrowing.
    availability: Literal["available", "unavailable", "unknown"]  # pyright: ignore[reportIncompatibleVariableOverride, reportGeneralTypeIssues]
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
        return cls.utc_timestamp(value)


class ChangeEvent(WireModel):
    id: Identity
    entity_id: Identity
    source_entity_id: Identity | None = None
    source_id: SourceId
    selection_revision: int = Field(ge=0)
    event_time: datetime
    received_at: datetime
    old: LightState | None = None
    new: LightState | None = None

    @field_validator("event_time", "received_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        return LightState.utc_timestamp(value)

    @model_validator(mode="after")
    def has_state(self) -> Self:
        if self.old is None and self.new is None:
            raise ValueError("An event needs an old or new state")
        return self


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
        return Observation.utc_timestamp(value)


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
        return LightState.utc_timestamp(value)


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


class BaselineRecord(LightReading):
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


class EventReading(LightReading):
    """The latest state taken from a recorded change event."""

    id: Identity
    kind: Literal["event"]
    entity_id: Identity
    source_entity_id: Identity | None
    source_id: SourceId
    event_time: Timestamp
    received_at: Timestamp
    persisted_at: Timestamp


# The newest state of a monitored light: a snapshot or a recorded change event.
CurrentReading = Annotated[BaselineRecord | EventReading, Field(discriminator="kind")]


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
    old: LightReading | None
    new: LightReading | None


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


class LightUsage(WireModel):
    entity_id: Identity
    on_seconds: float
    unknown_seconds: float
    switch_ons: int
    average_brightness: float | None
    days: list[DayUsage]
    # On-seconds per local hour of day, 0 to 23, summed over the whole range.
    hours: list[float] = Field(min_length=24, max_length=24)
    unknown_intervals: list[UnknownInterval]


class UsageReport(WireModel):
    start: Timestamp
    end: Timestamp
    timezone: str
    retained_since: Timestamp
    lights: list[LightUsage]
