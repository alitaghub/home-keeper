from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Identity = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
]
Name = Annotated[str, StringConstraints(min_length=1, max_length=256)]


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
    def unsupported_reason(self):
        if not self.supported and not self.reason:
            raise ValueError("Unsupported entities need a reason")
        return self


class Catalog(WireModel):
    source_id: Literal["demo", "home_assistant"]
    entities: list[Entity] = Field(max_length=500)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({entity.id for entity in self.entities}) != len(self.entities):
            raise ValueError("Duplicate entity identity")
        return self


class SelectionChange(WireModel):
    selected: bool


Channel = Annotated[int, Field(strict=True, ge=0, le=255)]
Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class LightState(WireModel):
    state: Literal["on", "off", "unavailable", "unknown"]
    last_changed: datetime | None = None
    last_updated: datetime | None = None
    last_reported: datetime | None = None
    brightness: int | None = Field(default=None, strict=True, ge=0, le=255)
    color_mode: (
        Literal[
            "unknown",
            "onoff",
            "brightness",
            "color_temp",
            "hs",
            "xy",
            "rgb",
            "rgbw",
            "rgbww",
            "white",
        ]
        | None
    ) = None
    color_temp_kelvin: int | None = Field(default=None, strict=True, ge=1000, le=10000)
    hs_color: tuple[Coordinate, Coordinate] | None = None
    xy_color: tuple[Coordinate, Coordinate] | None = None
    rgb_color: tuple[Channel, Channel, Channel] | None = None
    rgbw_color: tuple[Channel, Channel, Channel, Channel] | None = None
    rgbww_color: tuple[Channel, Channel, Channel, Channel, Channel] | None = None

    availability: Literal["available", "unavailable", "unknown"] | None = None

    @model_validator(mode="after")
    def state_availability(self):
        expected = "available" if self.state in ("on", "off") else self.state
        if self.availability is not None and self.availability != expected:
            raise ValueError("Availability contradicts state")
        self.availability = expected
        return self

    @field_validator("last_changed", "last_updated", "last_reported", check_fields=False)
    @classmethod
    def utc_timestamp(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Timestamp requires a timezone")
            return value.astimezone(UTC)
        return value

    @field_validator("rgb_color", "rgbw_color", "rgbww_color")
    @classmethod
    def rgb_range(cls, value):
        if value is not None and any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("RGB channels must be between 0 and 255")
        return value

    @field_validator("hs_color", "xy_color")
    @classmethod
    def coordinate_range(cls, value, info):
        if value is not None:
            bounds = (360, 100) if info.field_name == "hs_color" else (1, 1)
            if any(not 0 <= channel <= bound for channel, bound in zip(value, bounds, strict=True)):
                raise ValueError("Color coordinate outside supported range")
        return value

    def meaningful(self):
        return self.model_dump(
            exclude={"last_changed", "last_updated", "last_reported"}, exclude_none=True
        )


class Observation(LightState):
    # Availability is explicit on the existing snapshot protocol.
    availability: Literal["available", "unavailable", "unknown"]
    source_entity_id: Identity | None = None
    entity_id: Identity
    selection_revision: int = Field(ge=0)
    source_id: Literal["demo", "home_assistant"]
    kind: Literal["baseline", "reconciliation", "recovery"] = "baseline"
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def receipt_timestamp(cls, value):
        return cls.utc_timestamp(value)


class ChangeEvent(WireModel):
    id: Identity
    entity_id: Identity
    source_entity_id: Identity | None = None
    source_id: Literal["demo", "home_assistant"]
    selection_revision: int = Field(ge=0)
    event_time: datetime
    received_at: datetime
    old: LightState | None = None
    new: LightState | None = None

    @field_validator("event_time", "received_at")
    @classmethod
    def utc_timestamp(cls, value):
        return LightState.utc_timestamp(value)

    @model_validator(mode="after")
    def has_state(self):
        if self.old is None and self.new is None:
            raise ValueError("An event needs an old or new state")
        return self


class Health(WireModel):
    source_id: Literal["demo", "home_assistant"]
    status: Literal["healthy", "failed"]
    error: (
        Literal[
            "authentication",
            "connection",
            "tls",
            "timeout",
            "invalid_data",
            "missing",
            "discovery",
            "configuration",
        ]
        | None
    ) = None
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def utc_timestamp(cls, value):
        return Observation.utc_timestamp(value)


class Coverage(WireModel):
    source_id: Literal["demo", "home_assistant"]
    status: Literal["interrupted", "recovered"]
    reason: Literal[
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
    ] = "connection"
    received_at: datetime
    entity_id: Identity | None = None
    selection_revision: int | None = Field(default=None, ge=0)

    @field_validator("received_at")
    @classmethod
    def utc_timestamp(cls, value):
        return LightState.utc_timestamp(value)
